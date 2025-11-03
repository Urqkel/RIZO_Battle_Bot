FROM python:3.11-slim

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir fastapi uvicorn python-telegram-bot==21.6 pillow pytesseract jinja2

# Tesseract binary for OCR
RUN apt-get update && apt-get install -y tesseract-ocr && apt-get clean

ENV PORT=10000
EXPOSE 10000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "10000"]
