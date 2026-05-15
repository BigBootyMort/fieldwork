FROM python:3.11-slim

# Build tools needed by maigret's pycairo dependency (PDF report rendering)
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates \
      libcairo2-dev pkg-config meson ninja-build gcc \
    && rm -rf /var/lib/apt/lists/*

# Install maigret from GitHub source (v0.6.0 is not on PyPI)
# fastapi + uvicorn — thin HTTP wrapper so the backend can call it
RUN pip install --no-cache-dir \
    "git+https://github.com/soxoj/maigret.git@v0.6.0" \
    fastapi==0.115.0 \
    "uvicorn[standard]==0.30.6"

WORKDIR /srv
COPY server.py .

# Non-root user
RUN useradd -m -u 1000 app && chown -R app:app /srv
USER app

EXPOSE 7000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7000"]
