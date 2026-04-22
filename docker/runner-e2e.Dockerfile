# Runner para tests E2E con Playwright (browsers pre-instalados).
# La imagen base ya trae un usuario 'pwuser' con UID 1000.
FROM mcr.microsoft.com/playwright:v1.48.0-jammy

# Python (la imagen base solo trae Node)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# venv para aislar deps Python del sistema
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
    pytest==8.3.3 \
    pytest-playwright==0.5.2 \
    playwright==1.48.0 \
    requests==2.32.3

# Crear /workspace y dar ownership al usuario existente (pwuser, UID 1000)
RUN mkdir -p /workspace && chown pwuser:pwuser /workspace && \
    chown -R pwuser:pwuser /opt/venv

USER pwuser
WORKDIR /workspace

RUN git config --global user.email "runner@pipeline-ia.local" && \
    git config --global user.name "pipeline-ia runner" && \
    git config --global --add safe.directory '*'