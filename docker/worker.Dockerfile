# Prefect Worker: runs the flows that drive the pipeline.
#
# Extends the official Prefect image with:
#   - docker-cli + docker-py (to launch agent containers)
#   - the flow and orchestrator source code
#   - git (to create worktrees)
FROM prefecthq/prefect:3-latest

ARG HOST_UID=1000
ARG HOST_GID=1000

USER root

# Install docker-cli and git
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Extra Python dependencies on top of what Prefect ships
COPY docker/worker-requirements.txt /tmp/worker-requirements.txt
RUN pip install --no-cache-dir -r /tmp/worker-requirements.txt

# Pipeline source code
COPY flows/ /app/flows/
COPY orchestrator/ /app/orchestrator/
# In V2 the worker runs OpenHands directly (no per-role agent containers),
# so it needs the full `agents/` package — wrappers, prompts and the
# shared `models.py` contract.
COPY agents/ /app/agents/
COPY prefect.yaml /app/prefect.yaml

# Create runtime directories and hand them to the host user's UID/GID so
# the worker (running as that user at runtime) can write to them.
RUN mkdir -p /app/worktrees /app/logs /app/data && chown -R ${HOST_UID}:${HOST_GID} /app

RUN git config --system user.email "worker@atelier.local" && \
    git config --system user.name "atelier worker" && \
    git config --system --add safe.directory '*'

ENV PYTHONPATH=/app
