# Orchestrator based on the official Prefect image to avoid initialization
# bugs around Prefect's internal Pydantic models.
FROM prefecthq/prefect:3-latest

ARG HOST_UID=1000
ARG HOST_GID=1000

WORKDIR /app

# Install only what Prefect does NOT already ship.
# fastapi + uvicorn (pydantic already comes with prefect).
# jinja2 + markdown + python-multipart → HTML dashboard (option A).
RUN pip install --no-cache-dir \
    fastapi==0.115.4 \
    "uvicorn[standard]==0.32.0" \
    jinja2==3.1.4 \
    markdown==3.7 \
    python-multipart==0.0.12 \
    "cryptography>=42"

COPY orchestrator/ /app/orchestrator/
COPY agents/models.py /app/agents/models.py
COPY agents/__init__.py /app/agents/__init__.py

RUN mkdir -p /app/logs /app/data /app/.prefect && chown -R ${HOST_UID}:${HOST_GID} /app

ENV PYTHONPATH=/app
ENV PREFECT_HOME=/app/.prefect

EXPOSE 8000

CMD ["sh", "-c", "mkdir -p /app/logs /app/.prefect && exec uvicorn orchestrator.main:app --host 0.0.0.0 --port 8000"]
