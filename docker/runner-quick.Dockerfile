# Runner for quick and full tests.
# Supports Python projects (pytest) and JS/TS (jest, vitest, npm test).
#
# Does NOT include browsers — that is what atelier-runner-e2e is for.

FROM python:3.12-slim

ARG HOST_UID=1000
ARG HOST_GID=1000

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    gnupg \
    build-essential \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Common test tooling pre-installed — saves seconds per task
RUN pip install --no-cache-dir \
    pytest==8.3.3 \
    pytest-cov==5.0.0 \
    ruff==0.7.2 \
    mypy==1.13.0 \
    flask==3.0.3 \
    fastapi==0.115.4 \
    httpx==0.27.2 \
    requests==2.32.3

# pnpm and yarn in case JS projects use them
RUN npm install -g pnpm yarn

RUN groupadd -o -g ${HOST_GID} runner \
    && useradd -m -o -u ${HOST_UID} -g ${HOST_GID} runner \
    && mkdir -p /workspace \
    && chown ${HOST_UID}:${HOST_GID} /workspace

USER runner
WORKDIR /workspace

RUN git config --global user.email "runner@atelier.local" && \
    git config --global user.name "atelier runner" && \
    git config --global --add safe.directory '*'

# No entrypoint: the caller invokes the runner with "sh -c '<command>'"
