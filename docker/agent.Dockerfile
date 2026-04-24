# Single image shared by the three agent roles (implementer, reviewer, simplifier).
FROM python:3.12-slim

ARG HOST_UID=1000
ARG HOST_GID=1000

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# `-o` allows duplicate (non-unique) UID/GID — needed when HOST_UID collides
# with an existing system user in the base image.
RUN groupadd -o -g ${HOST_GID} agent \
    && useradd -m -o -u ${HOST_UID} -g ${HOST_GID} agent
WORKDIR /app

COPY docker/agent-requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY agents/ /app/agents/
RUN chown -R ${HOST_UID}:${HOST_GID} /app

USER agent

RUN git config --global user.email "agent@atelier.local" && \
    git config --global user.name "atelier agent" && \
    git config --global --add safe.directory '*'

# Stop LiteLLM/Aider from making external HTTP requests on startup
# (they hang on timeouts when the network cannot reach certain CDNs).
# OPENAI_API_BASE / OPENAI_API_KEY are injected per-container by the worker.
ENV PYTHONPATH=/app
ENV LITELLM_LOCAL_MODEL_COST_MAP=True
ENV AIDER_ANALYTICS=false
ENV DISABLE_TELEMETRY=1

ENTRYPOINT ["python", "-m", "agents.entrypoint"]
