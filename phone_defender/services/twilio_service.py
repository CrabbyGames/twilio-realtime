from loguru import logger
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse


class TwilioService:
    def __init__(self, account_sid: str, auth_token: str):
        self.client = Client(account_sid, auth_token) if account_sid and auth_token else None

    async def forward_call(self, call_sid: str, phone_number: str, base_url: str) -> bool:
        if not call_sid or not self.client:
            return False

        try:
            forward_url = f"{base_url}/forward-call?forward_to={phone_number}"
            self.client.calls(call_sid).update(method="GET", url=forward_url)
            return True
        except Exception as e:
            logger.exception(f"Error forwarding call")
            return False

    async def hangup_call(self, call_sid: str) -> bool:
        if not call_sid or not self.client:
            return False

        try:
            call = self.client.calls(call_sid).update(status="completed")
            logger.success(f"Call status updated to: {call.status}")
            return True
        except Exception as e:
            logger.exception(f"Error hanging up call")
            return False

    def create_connect_response(self, websocket_url: str) -> VoiceResponse:
        response = VoiceResponse()
        connect = Connect()
        connect.stream(url=websocket_url)
        response.append(connect)
        return response
