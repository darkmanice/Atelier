# Prefect Worker: ejecuta los flows que definen el pipeline.
#
# Extiende la imagen oficial de Prefect con:
#   - docker-cli + docker-py (para lanzar contenedores-agente)
#   - tu código de flows y orchestrator
#   - git (para crear worktrees)
FROM prefecthq/prefect:3-latest

USER root

# Instalar docker-cli y git
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencias Python adicionales sobre lo que trae Prefect
COPY docker/worker-requirements.txt /tmp/worker-requirements.txt
RUN pip install --no-cache-dir -r /tmp/worker-requirements.txt

# Código del pipeline
COPY flows/ /app/flows/
COPY orchestrator/ /app/orchestrator/
COPY agents/models.py /app/agents/models.py
COPY agents/__init__.py /app/agents/__init__.py
COPY prefect.yaml /app/prefect.yaml

# Crear directorios runtime y dar permisos al UID 1000 (el que corre)
RUN mkdir -p /app/worktrees /app/logs /app/data && chown -R 1000:1000 /app

RUN git config --system user.email "worker@pipeline-ia.local" && \
    git config --system user.name "pipeline-ia worker" && \
    git config --system --add safe.directory '*'

ENV PYTHONPATH=/app
