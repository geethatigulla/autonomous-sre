# Orchestrator image: FastAPI control plane + dashboard + demo CLI.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git is required by the Verifier sandbox (git init / git apply).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock ./
RUN pip install -r requirements.lock

COPY orchestrator ./orchestrator
# The Verifier copies victim_app/ into a tempdir and runs its tests.
COPY victim_app ./victim_app
COPY scripts ./scripts
COPY omium.toml ./

RUN useradd --create-home --uid 1000 sre \
    && mkdir -p /data \
    && chown -R sre:sre /app /data
USER sre

ENV DATABASE_URL=sqlite:////data/sre.db \
    LOCAL_TRACE_PATH=/data/local_traces.jsonl

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8000"]
