FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils tesseract-ocr espeak-ng ffmpeg && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir 'faster-whisper>=1.1,<2'
# faster-whisper 1.2.1 still passes metadata_errors to av.open; PyAV 19 removed it.
RUN pip install --no-cache-dir 'av>=11,<19'
WORKDIR /app
CMD ["python", "/app/multimodal_service.py"]
