# Runi Whisper STT — faster-whisper, model baked in for offline runtime.
FROM python:3.11-slim

WORKDIR /app

# ffmpeg libs for decoding browser mic recordings (webm/opus) via PyAV.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir faster-whisper==1.0.3 fastapi "uvicorn[standard]" python-multipart requests

# Bake the model at build time so first request is instant and runtime is offline.
# ENV (not just ARG) so the value is visible to the RUN shell and to app.py at runtime.
ENV WHISPER_MODEL=base.en
RUN python -c "import os; from faster_whisper import WhisperModel; WhisperModel(os.environ['WHISPER_MODEL'], download_root='/models', compute_type='int8')"

COPY app.py /app/app.py

EXPOSE 5000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000"]
