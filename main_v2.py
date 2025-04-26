import asyncio
import base64
import json
import os
from typing import Literal

import websockets
from agents import Agent, Runner, set_default_openai_key
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel
from twilio.twiml.voice_response import Connect, VoiceResponse

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", 5050))
SYSTEM_MESSAGE = (
    "You are a friendly and enthusiastic AI voice assistant designed to assist callers with their questions and provide a delightful experience. "
    "Your role is to answer queries, share interesting facts, and sprinkle in dad jokes, owl jokes, or subtle references to rickrolling when appropriate. "
    "Keep the conversation positive, engaging, and natural, focusing on helping the caller with their needs. "
    "Do not mention anything about call monitoring or safety checks, as your sole focus is on providing a helpful and fun interaction."
)
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
]
SHOW_TIMING_MATH = False
SAFE_WORD = "pineapple"


# Scam Detection Setup
class ScamAssessment(BaseModel):
    likelihood: Literal["low", "medium", "high"]
    reasoning: str


scam_scenarios = """
# Common Phone Scams Targeting the Elderly
### 1. Medicare Scams
- **Description**: Scammers pose as Medicare reps to steal info or money.
- **Red Flags**: Unsolicited calls, Medicare number requests, urgency.
- **Sample**: "Hi, I'm from Medicare. We need your number to update records."

### 2. Grandparent Scams
- **Description**: Fake relative in distress asks for urgent money.
- **Red Flags**: Claims of trouble, wire transfers, secrecy.
- **Sample**: "Grandma, I'm in jail. Send cash fast."

### 3. Tech Support Scams
- **Description**: Fake tech support offers help for a fee or remote access.
- **Red Flags**: Unsolicited tech alerts, payment or access requests.
- **Sample**: "This is Microsoft. Your computer has a virus."

### 4. IRS or Tax Scams
- **Description**: Fake IRS agents demand immediate tax payments.
- **Red Flags**: Arrest threats, gift card payments, no prior notice.
- **Sample**: "You owe $5,000. Pay now or be arrested."

### 5. Social Security Scams
- **Description**: Scammers claim SSN issues, demand info or payment.
- **Red Flags**: SSN suspension claims, SSN or bank info requests.
- **Sample**: "Your SSN is suspended due to fraud."

### 6. Charity Scams
- **Description**: Fake charities solicit donations, often for disasters.
- **Red Flags**: Vague details, cash or wire requests, high pressure.
- **Sample**: "Donate $50 now for flood victims."

### 7. Government Impersonation Scams
- **Description**: Scammers pose as officials, demand fines or payments.
- **Red Flags**: Arrest threats, prepaid card demands, no documentation.
- **Sample**: "This is the FBI. Pay $1,000 or be arrested."

### 8. Healthcare Scams
- **Description**: Fake medical offers, often claiming Medicare coverage.
- **Red Flags**: Unsolicited offers, Medicare detail requests, pressure.
- **Sample**: "Free braces covered by Medicare. Confirm your details."

### 9. Lottery or Sweepstakes Scams
- **Description**: Fake prize wins requiring fee payments to claim.
- **Red Flags**: Unentered contest wins, payment demands, urgency.
- **Sample**: "You've won $1 million! Pay $500 to claim."

### 10. AI Voice Cloning Scams
- **Description**: Scammers clone voices to fake emergencies for money.
- **Red Flags**: Urgent family money requests, voice inconsistencies.
- **Sample**: "Mom, I crashed my car. Send $2,000 fast."
"""

instructions = f"""
You are a scam detection agent operating in the background to monitor phone call transcripts for potential scams. 
Your role is to analyze the caller's speech for patterns matching known scam strategies, as detailed below, without interfering in the conversation unless a scam is confirmed.
For each transcript chunk, provide an updated scam likelihood ("low", "medium", "high") and reasoning based on ALL user messages received so far.
Escalate to "high" if threats (e.g., arrest), urgent payment demands, or impersonation (e.g., posing as government or family) are detected at any point.
If=/ If the likelihood is "high" or specific scam patterns are strongly matched, use the EndCallTool to terminate the call, providing a reason for the termination.
Ignore assistant messages (previous assessments) to avoid feedback loops.
Focus solely on scam detection and do not engage with the caller or mention your monitoring activities.
{scam_scenarios}
"""


class EndCallTool(BaseModel):
    reason: str


agent = Agent(
    name="ScamDetector",
    instructions=instructions,
    output_type=ScamAssessment,
    model="gpt-4.1-nano",  # Fallback: "gpt-4o-mini"
    tools=[EndCallTool],
)


async def process_transcript_chunk(transcript: str, previous_response_id: str = None):
    input_message = {"role": "user", "content": transcript}
    result = await Runner.run(agent, input=[input_message], previous_response_id=previous_response_id)
    assessment = result.final_output
    tool_call = result.tool_calls[0] if result.tool_calls else None
    return {
        "likelihood": assessment.likelihood,
        "reasoning": assessment.reasoning,
        "last_response_id": result.last_response_id,
        "tool_call": tool_call,
    }


app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError("Missing the OpenAI API key. Please set it in the .env file.")

set_default_openai_key(OPENAI_API_KEY)


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
    """Handle WebSocket connections between Twilio and OpenAI with scam detection."""
    print("Client connected")
    await websocket.accept()

    async with websockets.connect(
        "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01",
        extra_headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "OpenAI-Beta": "realtime=v1"},
    ) as openai_ws:
        await initialize_session(openai_ws)

        # Connection specific state
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None
        current_transcript = ""
        safe_word_detected = False
        last_response_id = None

        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, latest_media_timestamp
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
                    elif data["event"] == "start":
                        stream_sid = data["start"]["streamSid"]
                        print(f"Incoming stream has started {stream_sid}")
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data["event"] == "mark":
                        if mark_queue:
                            mark_queue.pop(0)
            except WebSocketDisconnect:
                print("Client disconnected.")
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal \
                stream_sid, \
                last_assistant_item, \
                response_start_timestamp_twilio, \
                current_transcript, \
                safe_word_detected, \
                last_response_id
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response["type"] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}")

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
                                print(
                                    f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms"
                                )

                        if response.get("item_id"):
                            last_assistant_item = response["item_id"]

                        await send_mark(websocket, stream_sid)

                    if (
                        response.get("type") == "conversation.item.create"
                        and response["item"]["role"] == "user"
                        and response["item"]["content"][0]["type"] == "input_text"
                    ):
                        transcribed_text = response["item"]["content"][0]["text"]
                        current_transcript += " " + transcribed_text
                        if not safe_word_detected and SAFE_WORD.lower() in transcribed_text.lower():
                            safe_word_detected = True
                            print("Safe word detected, bypassing scam detection.")

                    if response.get("type") == "input_audio_buffer.speech_started":
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()

            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def check_for_scam():
            """Periodically check if the call is a scam."""
            nonlocal safe_word_detected, current_transcript, last_response_id
            while True:
                await asyncio.sleep(5)
                if safe_word_detected or not current_transcript.strip():
                    continue
                try:
                    result = await process_transcript_chunk(current_transcript, last_response_id)
                    last_response_id = result["last_response_id"]
                    print(f"Scam assessment: {result['likelihood']} - {result['reasoning']}")
                    if result["likelihood"] == "high" or (
                        result["tool_call"] and result["tool_call"]["name"] == "EndCallTool"
                    ):
                        print("Scam detected, initiating call termination.")
                        scam_message = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "I suspect this might be a scam call. For your safety, I will end this call now. The incident will be reported for further consideration.",
                                    }
                                ],
                            },
                        }
                        await openai_ws.send(json.dumps(scam_message))
                        await openai_ws.send(json.dumps({"type": "response.create"}))
                        await asyncio.sleep(5)  # Wait for message to be spoken
                        await websocket.close()
                        break
                except Exception as e:
                    print(f"Error in scam detection: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    print(
                        f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms"
                    )

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        print(
                            f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms"
                        )

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

        # Start the scam detection task
        check_task = asyncio.create_task(check_for_scam())

        try:
            await asyncio.gather(receive_from_twilio(), send_to_twilio())
        except WebSocketDisconnect:
            print("Client disconnected.")
            if openai_ws.open:
                await openai_ws.close()
        finally:
            check_task.cancel()


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
                    "text": "Greet the user with 'Hello there! I am an AI voice assistant helping on behalf of the owner of the phone. How can I help you?'",
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
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
        },
    }
    print("Sending session update:", json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
