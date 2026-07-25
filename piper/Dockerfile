# Runi Piper TTS — uses the official Piper *binary* (not the pip wheel), which
# bundles a complete, matching espeak-ng-data. The pip piper-phonemize wheel
# fails with "Failed to set eSpeak-ng voice" on this platform, mangling
# pronunciation; the binary phonemizes correctly.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi "uvicorn[standard]"

# Official Piper binary + its bundled espeak-ng-data and shared libs → /opt/piper/
RUN curl -sL "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz" \
      -o /tmp/piper.tar.gz \
    && tar -xzf /tmp/piper.tar.gz -C /opt \
    && rm /tmp/piper.tar.gz
ENV LD_LIBRARY_PATH=/opt/piper

# Voice models baked in (offline runtime). Two British-female voices to A/B.
ADD https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium.onnx      /voices/en_GB-jenny_dioco-medium.onnx
ADD https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium.onnx.json /voices/en_GB-jenny_dioco-medium.onnx.json
ADD https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alba/medium/en_GB-alba-medium.onnx           /voices/en_GB-alba-medium.onnx
ADD https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alba/medium/en_GB-alba-medium.onnx.json      /voices/en_GB-alba-medium.onnx.json

COPY app.py /app/app.py

EXPOSE 5000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000"]
