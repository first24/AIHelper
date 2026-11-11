FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 assistant \
    && useradd --uid 10001 --gid assistant --no-create-home assistant \
    && mkdir /data && chown assistant:assistant /data
COPY --chown=assistant:assistant assistant/ ./assistant/
USER assistant
ENV ASSISTANT_DB=/data/assistant.sqlite3 USE_GIT_LOGIN=false
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-m", "assistant", "health"]
CMD ["python", "-m", "assistant", "run"]
