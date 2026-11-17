# Dockerfile for the victim app.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install only what the victim app needs. Kept minimal so the image rebuilds
# fast when the orchestrator iterates on patches.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app.py monitor.py scrubber.py ./

EXPOSE 9000

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/healthz', timeout=2)" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
