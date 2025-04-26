import asyncio
import base64
import json

import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from phone_defender.services.openai_service import OpenAIService

router = APIRouter()


@router.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    settings = websocket.app.state.settings
    twilio_service = websocket.app.state.twilio_service
    openai_service = OpenAIService(settings)

    async with websockets.connect(
        "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01",
        extra_headers={"Authorization": f"Bearer {settings.openai_api_key}", "OpenAI-Beta": "realtime=v1"},
    ) as openai_ws:
        await openai_service.initialize_session(openai_ws)

        stream_sid = None
        call_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None

        async def receive_from_twilio():
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
                    elif data["event"] == "start":
                        stream_sid = data["start"]["streamSid"]
                        call_sid = data["start"].get("callSid")
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data["event"] == "mark":
                        if mark_queue:
                            mark_queue.pop(0)
            except WebSocketDisconnect:
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            nonlocal stream_sid, call_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response["type"] in settings.log_event_types:
                        logger.debug(f"Received event: {response['type']}", response)

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
                            if settings.show_timing_math:
                                logger.info(
                                    f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms"
                                )

                        if response.get("item_id"):
                            last_assistant_item = response["item_id"]

                        await send_mark(websocket, stream_sid)

                    if response.get("type") == "response.function_call_arguments.done":
                        function_name = response.get("name")
                        call_id = response.get("call_id")
                        arguments = response.get("arguments", "{}")

                        if function_name == "hangupCall":
                            success = await twilio_service.hangup_call(call_sid)
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

                        elif function_name == "forwardCall":
                            args = json.loads(arguments)
                            forward_number = args.get("phoneNumber", settings.adam_phone_number)
                            success = await twilio_service.forward_call(
                                call_sid, forward_number, settings.base_url
                            )
                            result = {"success": success, "forwardedTo": forward_number}
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
                        if last_assistant_item:
                            await handle_speech_started_event()
            except Exception as e:
                logger.exception(f"Error in send_to_twilio")

        async def handle_speech_started_event():
            nonlocal response_start_timestamp_twilio, last_assistant_item
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if settings.show_timing_math:
                    logger.info(
                        f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms"
                    )

                if last_assistant_item:
                    if settings.show_timing_math:
                        logger.info(
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

        await asyncio.gather(receive_from_twilio(), send_to_twilio())
