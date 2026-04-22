# Runner para tests quick y full.
# Soporta proyectos Python (pytest) y JS/TS (jest, vitest, npm test).
#
# No incluye browsers — para eso está pipeline-runner-e2e.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    gnupg \
    build-essential \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Herramientas de test comunes que tener pre-instaladas ahorra segundos por tarea
RUN pip install --no-cache-dir \
    pytest==8.3.3 \
    pytest-cov==5.0.0 \
    ruff==0.7.2 \
    mypy==1.13.0 \
    flask==3.0.3 \
    fastapi==0.115.4 \
    httpx==0.27.2 \
    requests==2.32.3

# pnpm y yarn por si los proyectos JS los usan
RUN npm install -g pnpm yarn

RUN useradd -m -u 1000 runner && mkdir -p /workspace && chown runner:runner /workspace

USER runner
WORKDIR /workspace

RUN git config --global user.email "runner@pipeline-ia.local" && \
    git config --global user.name "pipeline-ia runner" && \
    git config --global --add safe.directory '*'

# Entrypoint vacío: el runner se invoca con "sh -c '<comando>'" por el caller
