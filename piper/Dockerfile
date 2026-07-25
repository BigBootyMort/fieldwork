# Runi Piper TTS — local neural voice, models baked in for offline runtime.
FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir piper-tts==1.2.0 fastapi "uvicorn[standard]"

# Bake voice models at build time (Docker's build network reaches HuggingFace).
# Two British-female voices to A/B for Runi's character:
#   jenny_dioco — warmer, expressive     alba — cooler, composed
ADD https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium.onnx      /voices/en_GB-jenny_dioco-medium.onnx
ADD https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium.onnx.json /voices/en_GB-jenny_dioco-medium.onnx.json
ADD https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alba/medium/en_GB-alba-medium.onnx           /voices/en_GB-alba-medium.onnx
ADD https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alba/medium/en_GB-alba-medium.onnx.json      /voices/en_GB-alba-medium.onnx.json

COPY app.py /app/app.py

EXPOSE 5000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000"]
