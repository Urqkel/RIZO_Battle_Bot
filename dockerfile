FROM python:3.11-slim

# ---------- Environment setup ----------
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ---------- Install system dependencies for Pillow, tesseract, and utility programs ----------
# Removed 'build-essential' as it is often not needed on slim for runtime, but kept Tesseract dependencies.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        libsm6 \
        libxrender1 \
        libxext6 \
        tesseract-ocr \
        tesseract-ocr-eng \
        poppler-utils \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ---------- Set workdir ----------
WORKDIR /app

# ---------- Copy files ----------
COPY requirements.txt .
COPY app.py .
COPY static/ ./static
COPY templates/ ./templates

# ---------- Install Python dependencies ----------
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# ---------- Expose port ----------
EXPOSE 10000

# ---------- Set environment variables (Placeholder) ----------
ENV BOT_TOKEN=""
ENV RENDER_EXTERNAL_URL=""

# ---------- Start the bot (Shell form CMD used for variable substitution) ----------
# We use the shell form CMD to ensure the environment variable $PORT is correctly
# substituted into the command before Uvicorn runs.
CMD uvicorn app:app --host 0.0.0.0 --port $PORT
