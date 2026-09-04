# Text-on-image MCP server. Build context = the text-mcp/ directory.
# Pinned base = exact interpreter of the verified local venv.
FROM python:3.14.7-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TOI_DATA=/data \
    TOI_MEDIA_ROOT=/data/media

WORKDIR /app

# Dependencies first for layer caching; exact pins, no resolve drift.
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# Application: runtime modules + fonts (18 MB, no runtime downloads).
# tests/.venv/state files are excluded via .dockerignore.
COPY server.py scene.py render.py ./
COPY fonts/ ./fonts/

RUN useradd --create-home --uid 10001 toi \
    && mkdir -p /data/media \
    && chown -R toi:toi /data /app
USER toi:toi

EXPOSE 8080
VOLUME ["/data"]

# TCP connect to the MCP port: process alive and listening.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8080), 4).close()"

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8080"]