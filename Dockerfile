# Jarvis reporting backend — container image for 24/7 hosting (Fly.io / Render / any Docker host).
# Serves the FastAPI app (server:app) which also serves the reporting UI at /app.
#
# NOTE: macOS-only features (Apple Calendar/Mail/Notes via AppleScript, opening
# local apps) do not run in Linux — the reporting suite, brain loop, BigQuery,
# auth, and share links do. Set secrets as env vars (see DEPLOY.md).

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    JARVIS_REQUIRE_AUTH=1

WORKDIR /app

# System deps some Python wheels want (kept minimal).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt uvicorn

# App source (respects .dockerignore).
COPY . .

# The served UI copy lives at ~/Downloads/martin_app.html locally; in the container
# we serve the repo copy — symlink it where the app expects it.
RUN mkdir -p /root/Downloads && ln -sf /app/martin_app.html /root/Downloads/martin_app.html || true

EXPOSE 8000

# Host provides TLS; run plain uvicorn against the FastAPI app object.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
