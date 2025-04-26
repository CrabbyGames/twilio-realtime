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




# Use Python 3.11 as the base image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Expose the port the app runs on
EXPOSE 5050

# Command to run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5050"] 