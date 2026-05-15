FROM python:3.11-slim

# System tools used by media analysis endpoints
RUN apt-get update && apt-get install -y --no-install-recommends \
    libimage-exiftool-perl \
    ffmpeg \
    # Phase 11 — WeasyPrint PDF rendering dependencies
    libpangocairo-1.0-0 \
    libpango-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Phase 4: download spaCy NLP model and TextBlob corpora at build time
# so the first request doesn't pay the download cost at runtime.
# en_core_web_sm is ~12 MB — fast, good enough for NER on news text.
# Swap for en_core_web_md/lg for higher accuracy at the cost of size.
RUN pip install --no-cache-dir \
    https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.7.1/en_core_web_sm-3.7.1-py3-none-any.whl
RUN python -m textblob.download_corpora

COPY ./app /app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
