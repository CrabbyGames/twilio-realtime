FROM python:3.11-slim

WORKDIR /app

RUN pip install uv

RUN uv venv /venv

ENV PATH=/venv/bin:$PATH

RUN uv pip install fastapi uvicorn websockets twilio python-dotenv

COPY main.py /app/main.py

ENV PORT=5050

EXPOSE 5050

CMD ["python", "main.py"]
