# Runner for E2E tests with Playwright (browsers pre-installed).
# Weighs ~1.5 GB but is only downloaded/built once.
#
# Supports both Python (playwright-python, pytest-playwright) and JS/TS
# (playwright/test).
#
# Base image: the official Playwright image with Chromium, Firefox, and
# WebKit preinstalled.

FROM mcr.microsoft.com/playwright:v1.48.0-jammy

# Python (the base image ships only Node)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# Python + Node test dependencies
RUN pip install --no-cache-dir --break-system-packages \
    pytest==8.3.3 \
    pytest-playwright==0.5.2 \
    playwright==1.48.0 \
    requests==2.32.3

RUN useradd -m -u 1000 runner && mkdir -p /workspace && chown runner:runner /workspace

USER runner
WORKDIR /workspace

RUN git config --global user.email "runner@atelier.local" && \
    git config --global user.name "atelier runner" && \
    git config --global --add safe.directory '*'
