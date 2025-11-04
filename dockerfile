FROM python:3.11-slim

# ---------- Environment setup ----------
# Ensure output is streamed immediately and Python doesn't write .pyc files
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ---------- Install system dependencies for Pillow, tesseract, and utility programs ----------
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libglib2.0-0 \
        libsm6 \
        libxrender1 \
        libxext6 \
        tesseract-ocr \
        tesseract-ocr-eng \
        poppler-utils \
        git \
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
# Render will look for the PORT env variable, but we expose 10000 as a default.
EXPOSE 10000

# ---------- Set environment variables (Placeholder) ----------
ENV BOT_TOKEN=""
ENV RENDER_EXTERNAL_URL=""

# ---------- Start the bot with Uvicorn, binding to 0.0.0.0 and the $PORT environment variable ----------
# This ensures Uvicorn listens on the port specified by the hosting environment (e.g., Render)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "${PORT:-10000}"]
