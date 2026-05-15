FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /spiderfoot

# Clone SpiderFoot source (no PyPI package exists)
RUN git clone --depth 1 https://github.com/smicallef/spiderfoot.git . \
    && pip install --no-cache-dir -r requirements.txt

RUN useradd -m -u 1000 -s /bin/sh spiderfoot \
    && chown -R spiderfoot:spiderfoot /spiderfoot

USER spiderfoot

EXPOSE 5001
CMD ["python", "sf.py", "-l", "0.0.0.0:5001"]
