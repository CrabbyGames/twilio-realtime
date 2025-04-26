from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()


@router.get("/", response_class=JSONResponse)
async def index():
    return {"message": "Twilio Media Stream Server is running!"}


@router.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    twilio_service = request.app.state.twilio_service
    host = request.url.hostname
    response = twilio_service.create_connect_response(f"wss://{host}/media-stream")
    return HTMLResponse(content=str(response), media_type="application/xml")
