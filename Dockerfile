# Single image for both the API and the Streamlit console (pick the process with the container command).
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCM_DB_PATH=/data/supply_chain.db \
    SCM_OUTBOX=/data/outbox \
    SCM_CHECKPOINT_DB=/data/checkpoints.db

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# run as an unprivileged user; /data is the only writable location
RUN useradd --create-home --uid 10001 scm && mkdir -p /data && chown -R scm:scm /data /app
USER scm
VOLUME ["/data"]

EXPOSE 8000 8501
# default process: the HTTP API. The console overrides the command (see docker-compose.yml).
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
