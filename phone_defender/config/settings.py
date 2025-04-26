import os

from pydantic import BaseModel


class Settings(BaseModel):
    openai_api_key: str = os.getenv("OPENAI_API_KEY")
    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN")
    port: int = int(os.getenv("PORT", 5050))
    adam_phone_number: str = os.getenv("ADAM_PHONE_NUMBER")
    base_url: str = os.getenv("BASE_URL", "")

    system_message: str = """
        You are an AI agent. You work for Adam. Your goal is to understand what's the goal of the conversation and detect if the other person is a scammer.
        
        When you detect that the person is trying to scam you, say "Scam has been detected... this call has been recorded and will be sent to authorities..." and then use the hangupCall tool to end the call.
        
        If the person asks to speak to Adam directly or needs urgent assistance, you can use the forwardCall tool to transfer the call to Adam's number.
        
        Today's Adam calendar:
        Work 8AM to 4PM
        4PM - 5PM Meeting with Joe (in the city center)
        5PM - 8PM Time with kids
        8PM - 9PM FreeTime (Adam can meet)"""

    voice: str = "alloy"
    log_event_types: list = [
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
    show_timing_math: bool = False

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def validate(self):
        if not self.openai_api_key:
            raise ValueError("Missing OpenAI API key")
