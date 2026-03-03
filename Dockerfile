FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

# Install system dependencies for OCR
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    poppler-utils \
    libgl1 \
    libglib2.0-0 \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy full project
COPY . .

# Start FastAPI using Render dynamic PORT
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port $PORT"]