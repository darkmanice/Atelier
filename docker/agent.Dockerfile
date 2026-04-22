# Imagen única para los tres roles de agente.
# Idéntica a la versión que ya funciona en pipeline-ia.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 agent
WORKDIR /app

COPY docker/agent-requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY agents/ /app/agents/
RUN chown -R agent:agent /app

USER agent

RUN git config --global user.email "agent@pipeline-ia.local" && \
    git config --global user.name "pipeline-ia agent" && \
    git config --global --add safe.directory '*'

# Evitar que LiteLLM/Aider hagan peticiones HTTP externas al arrancar
# (bloquean en timeouts cuando la red no puede llegar a ciertos CDN)
ENV PYTHONPATH=/app
ENV OLLAMA_API_BASE=http://ollama:11434
ENV LITELLM_LOCAL_MODEL_COST_MAP=True
ENV AIDER_ANALYTICS=false
ENV DISABLE_TELEMETRY=1

ENTRYPOINT ["python", "-m", "agents.entrypoint"]
