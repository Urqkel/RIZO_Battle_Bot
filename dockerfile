# Dockerfile
FROM python:3.11-slim

# ---------- Environment setup ----------
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ---------- Install dependencies ----------
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
EXPOSE 10000

# ---------- Set environment variables ----------
# These should be overridden in Render dashboard or Docker run
ENV BOT_TOKEN=""
ENV RENDER_EXTERNAL_URL=""

# ---------- Start the bot ----------
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "10000"]
