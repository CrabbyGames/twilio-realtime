import asyncio
import base64
import json
import os
import queue
import threading

import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
PORT = int(os.getenv("PORT", 5050))
TALKATIVE_SYSTEM_MESSAGE = """
    You are an AI voice assistant. Engage naturally with the caller. If instructed to end the call, say: 'Scam has been detected... this call has been recorded and will be send to authorities...' then use the hangupCall tool to end the call.
    Today's calendar:
    Work 8AM to 4PM
    4PM - 5PM Meeting with Joe (in the city center)
    5PM - 8PM Time with kids
    8PM - 9PM FreeTime (can meet)"""
MONITORING_SYSTEM_MESSAGE = """
    You are a scam detection agent. Analyze the conversation audio in real-time to assess the probability (0-100%) that it is a scam. Focus on intent, tone, urgency, and requests for sensitive information (e.g., passwords, bank details). Do not generate explicit transcriptions for analysis; use the audio context directly. Output a JSON object with 'probability' (float) and 'reason' (string explaining the assessment). Collect conversation text outputs for saving as a transcript later. Example output:
    {
        "probability": 0.85,
        "reason": "Caller urgently requested bank account details, a common scam tactic."
    }
"""
VOICE = "alloy"
LOG_EVENT_TYPES = [
    "error",
    "response.content.done",
    "rate_limits.updated",
    "response.done",
    "input_audio_buffer.committed",
    "input_audio_buffer.speech_stopped",
    "input_audio_buffer.speech_started",
    "session.created",
    "response.function_call_arguments.done",
]
SHOW_TIMING_MATH = False
SCAM_THRESHOLD = 0.9  # 90% probability threshold

app = FastAPI()
twilio_client = None

if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

if not OPENAI_API_KEY:
    raise ValueError("Missing the OpenAI API key. Please set it in the .env file.")

# Thread-safe storage for transcripts and call termination flags
transcripts = {}
transcripts_lock = threading.Lock()
terminate_calls = {}
terminate_calls_lock = threading.Lock()


def save_transcript(call_sid):
    """Save the transcript to a file."""
    with transcripts_lock:
        if call_sid in transcripts:
            transcript_text = " ".join(transcripts[call_sid])
            with open(f"transcript_{call_sid}.txt", "w") as f:
                f.write(transcript_text)
            del transcripts[call_sid]


async def run_monitoring_agent(call_sid, audio_queue):
    """Run the monitoring agent with OpenAI Realtime API."""
    async with websockets.connect(
        "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01",
        extra_headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"},
    ) as monitor_ws:
        # Initialize monitoring session
        session_update = {
            "type": "session.update",
            "session": {
                "turn_detection": {"type": "server_vad"},
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "voice": VOICE,
                "instructions": MONITORING_SYSTEM_MESSAGE,
                "modalities": ["text"],  # No audio output needed
                "temperature": 0.7,
            },
        }
        await monitor_ws.send(json.dumps(session_update))

        conversation_buffer = []
        while True:
            audio_chunk = audio_queue.get()
            if audio_chunk is None:
                break
            audio_append = {
                "type": "input_audio_buffer.append",
                "audio": audio_chunk,
            }
            await monitor_ws.send(json.dumps(audio_append))

            async for message in monitor_ws:
                response = json.loads(message)
                if response["type"] in LOG_EVENT_TYPES:
                    print(f"Monitoring agent event: {response['type']}")

                # Collect conversation text for transcript
                if response.get("type") == "response.content.done" and "content" in response:
                    for content in response["content"]:
                        if content["type"] == "text" and content.get("text"):
                            text = content["text"]
                            try:
                                # Check if the text is a scam analysis result
                                result = json.loads(text)
                                if "probability" in result and "reason" in result:
                                    probability = result["probability"]
                                    print(f"Scam probability: {probability}, Reason: {result['reason']}")
                                    if probability >= SCAM_THRESHOLD:
                                        with terminate_calls_lock:
                                            terminate_calls[call_sid] = True
                                        break
                            except json.JSONDecodeError:
                                # Not a JSON object, so it's part of the conversation
                                with transcripts_lock:
                                    if call_sid not in transcripts:
                                        transcripts[call_sid] = []
                                    transcripts[call_sid].append(text)
                                conversation_buffer.append(text)

                # Trigger scam analysis after each speech segment
                if response.get("type") == "input_audio_buffer.speech_stopped":
                    analysis_item = {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "Analyze the recent conversation audio for scam probability.",
                                }
                            ],
                        },
                    }
                    await monitor_ws.send(json.dumps(analysis_item))
                    await monitor_ws.send(json.dumps({"type": "response.create"}))

        save_transcript(call_sid)


@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    response = VoiceResponse()
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f"wss://{host}/media-stream")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    print("Client connected")
    await websocket.accept()

    async with websockets.connect(
        "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01",
        extra_headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"},
    ) as openai_ws:
        await initialize_session(openai_ws)

        # Connection specific state
        stream_sid = None
        call_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None
        audio_queue = queue.Queue()

        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, call_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data["event"] == "media" and openai_ws.open:
                        latest_media_timestamp = int(data["media"]["timestamp"])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"],
                        }
                        await openai_ws.send(json.dumps(audio_append))
                        audio_queue.put(data["media"]["payload"])
                    elif data["event"] == "start":
                        stream_sid = data["start"]["streamSid"]
                        call_sid = data["start"].get("callSid")
                        print(f"Incoming stream started {stream_sid}, Call SID: {call_sid}")
                        asyncio.create_task(run_monitoring_agent(call_sid, audio_queue))
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data["event"] == "mark":
                        if mark_queue:
                            mark_queue.pop(0)
            except WebSocketDisconnect:
                print("Client disconnected.")
                audio_queue.put(None)
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, call_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response["type"] in LOG_EVENT_TYPES:
                        print(f"Talkative agent event: {response['type']}")

                    # Check if call should be terminated
                    with terminate_calls_lock:
                        if call_sid in terminate_calls and terminate_calls[call_sid]:
                            termination_item = {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "Scam has been detected... this call has been recorded and will be send to authorities...",
                                        }
                                    ],
                                },
                            }
                            await openai_ws.send(json.dumps(termination_item))
                            await openai_ws.send(json.dumps({"type": "response.create"}))
                            await asyncio.sleep(2)  # Ensure message is sent
                            # Trigger hangupCall tool
                            hangup_item = {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call",
                                    "call_id": f"call_{call_sid}",
                                    "name": "hangupCall",
                                    "arguments": {},
                                },
                            }
                            await openai_ws.send(json.dumps(hangup_item))
                            await openai_ws.send(json.dumps({"type": "response.create"}))
                            break

                    if response.get("type") == "response.audio.delta" and "delta" in response:
                        audio_payload = base64.b64encode(base64.b64decode(response["delta"])).decode("utf-8")
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": audio_payload},
                        }
                        await websocket.send_json(audio_delta)

                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp
                            if SHOW_TIMING_MATH:
                                print(f"Setting start timestamp: {response_start_timestamp_twilio}ms")

                        if response.get("item_id"):
                            last_assistant_item = response["item_id"]

                        await send_mark(websocket, stream_sid)

                    # Handle function calls from the AI
                    if response.get("type") == "response.function_call_arguments.done":
                        function_name = response.get("name")
                        call_id = response.get("call_id")

                        if function_name == "hangupCall":
                            success = await hangup_call(call_sid)
                            result = {"success": success}
                            result_message = {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": json.dumps(result),
                                },
                            }
                            await openai_ws.send(json.dumps(result_message))
                            await openai_ws.send(json.dumps({"type": "response.create"}))

                    if response.get("type") == "input_audio_buffer.speech_started":
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()

            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    print(
                        f"Elapsed time: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms"
                    )

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        print(f"Truncating item {last_assistant_item} at {elapsed_time}ms")
                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time,
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({"event": "clear", "streamSid": stream_sid})

                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, stream_sid):
            if stream_sid:
                mark_event = {"event": "mark", "streamSid": stream_sid, "mark": {"name": "responsePart"}}
                await connection.send_json(mark_event)
                mark_queue.append("responsePart")

        async def hangup_call(call_sid):
            """Hang up a Twilio call using the REST API."""
            if not call_sid or not twilio_client:
                print(
                    f"Unable to hang up call: call_sid={call_sid}, client_available={twilio_client is not None}"
                )
                return False
            try:
                print(f"Hanging up call: {call_sid}")
                call = twilio_client.calls(call_sid).update(status="completed")
                print(f"Call status updated to: {call.status}")
                return True
            except Exception as e:
                print(f"Error hanging up call: {e}")
                return False

        await asyncio.gather(receive_from_twilio(), send_to_twilio())


async def send_initial_conversation_item(openai_ws):
    """Send initial conversation item if AI talks first."""
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Greet the user with 'Hello there! I am an AI voice assistant powered by Twilio and the OpenAI Realtime API. You can ask me for facts, jokes, or anything you can imagine. How can I help you?'",
                }
            ],
        },
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    await openai_ws.send(json.dumps({"type": "response.create"}))


async def initialize_session(openai_ws):
    """Control initial session with OpenAI."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": TALKATIVE_SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
            "tools": [
                {
                    "type": "function",
                    "name": "hangupCall",
                    "description": "Hang up the current call when a scam is detected",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                }
            ],
        },
    }
    print("Sending session update:", json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
