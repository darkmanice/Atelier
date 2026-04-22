# Orchestrator basado en la imagen oficial de Prefect para evitar bugs de
# inicialización de los modelos Pydantic internos.
FROM prefecthq/prefect:3-latest

WORKDIR /app

# Solo instalamos lo EXTRA sobre lo que trae Prefect.
# fastapi + uvicorn (pydantic ya viene con prefect).
# jinja2 + markdown + python-multipart → dashboard HTML (opción A).
RUN pip install --no-cache-dir \
    fastapi==0.115.4 \
    "uvicorn[standard]==0.32.0" \
    jinja2==3.1.4 \
    markdown==3.7 \
    python-multipart==0.0.12

COPY orchestrator/ /app/orchestrator/
COPY agents/models.py /app/agents/models.py
COPY agents/__init__.py /app/agents/__init__.py

RUN mkdir -p /app/logs

ENV PYTHONPATH=/app
ENV PREFECT_HOME=/app/.prefect

EXPOSE 8000

CMD ["sh", "-c", "mkdir -p /app/logs /app/.prefect && exec uvicorn orchestrator.main:app --host 0.0.0.0 --port 8000"]