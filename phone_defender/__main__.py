import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

from phone_defender.config.settings import Settings
from phone_defender.routes import call_routes, websocket_routes
from phone_defender.services.twilio_service import TwilioService


def create_app():
    settings = Settings()

    app = FastAPI()

    twilio_service = TwilioService(
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
    )

    app.state.twilio_service = twilio_service
    app.state.settings = settings

    app.include_router(call_routes.router)
    app.include_router(websocket_routes.router)

    return app


app = create_app()

if __name__ == "__main__":
    settings = Settings()
    uvicorn.run(app, host="0.0.0.0", port=settings.port)
