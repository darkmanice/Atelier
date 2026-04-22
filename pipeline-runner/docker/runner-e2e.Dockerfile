# Runner para tests E2E con Playwright (browsers pre-instalados).
# Pesa ~1.5 GB, pero solo se descarga/construye una vez.
#
# Soporta tanto Python (playwright-python, pytest-playwright) como JS/TS
# (playwright/test).
#
# Imagen base: Playwright oficial con Chromium, Firefox, WebKit preinstalados.

FROM mcr.microsoft.com/playwright:v1.48.0-jammy

# Python (la imagen base solo trae Node)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# Dependencias de test Python + Node
RUN pip install --no-cache-dir --break-system-packages \
    pytest==8.3.3 \
    pytest-playwright==0.5.2 \
    playwright==1.48.0 \
    requests==2.32.3

RUN useradd -m -u 1000 runner && mkdir -p /workspace && chown runner:runner /workspace

USER runner
WORKDIR /workspace

RUN git config --global user.email "runner@pipeline-ia.local" && \
    git config --global user.name "pipeline-ia runner" && \
    git config --global --add safe.directory '*'
