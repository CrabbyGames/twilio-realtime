import json

from phone_defender.config.settings import Settings


class OpenAIService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def initialize_session(self, ws):
        session_update = {
            "type": "session.update",
            "session": {
                "turn_detection": {"type": "server_vad"},
                "input_audio_format": "g711_ulaw",
                "output_audio_format": "g711_ulaw",
                "voice": self.settings.voice,
                "instructions": self.settings.system_message,
                "modalities": ["text", "audio"],
                "temperature": 0.8,
                "tools": [
                    {
                        "type": "function",
                        "name": "hangupCall",
                        "description": "Hang up the current call when a scam is detected",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                    {
                        "type": "function",
                        "name": "forwardCall",
                        "description": "Forward the current call to Adam's phone number or a specified number",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "phoneNumber": {
                                    "type": "string",
                                    "description": "The phone number to forward the call to. Default is Adam's number if not provided.",
                                }
                            },
                            "required": [],
                        },
                    },
                ],
            },
        }
        await ws.send(json.dumps(session_update))

        await self.send_initial_conversation_item(ws)

    async def send_initial_conversation_item(self, ws):
        initial_conversation_item = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Greet the user with 'Hello there! I am Adam's AI assistant. How can I help you today?'",
                    }
                ],
            },
        }
        await ws.send(json.dumps(initial_conversation_item))
        await ws.send(json.dumps({"type": "response.create"}))
