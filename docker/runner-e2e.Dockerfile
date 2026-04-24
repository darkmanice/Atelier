# Runner for E2E tests with Playwright (browsers pre-installed).
# The base image already ships a 'pwuser' user with UID 1000; we replace it
# with a runner user tied to the host UID/GID so bind-mounted worktrees stay
# writable for the host user.
FROM mcr.microsoft.com/playwright:v1.48.0-jammy

ARG HOST_UID=1000
ARG HOST_GID=1000

# Python (the base image ships only Node)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# venv to isolate Python deps from the system
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
    pytest==8.3.3 \
    pytest-playwright==0.5.2 \
    playwright==1.48.0 \
    requests==2.32.3

# Create /workspace and a user matching the host UID/GID
RUN groupadd -o -g ${HOST_GID} runner \
    && useradd -m -o -u ${HOST_UID} -g ${HOST_GID} runner \
    && mkdir -p /workspace \
    && chown ${HOST_UID}:${HOST_GID} /workspace \
    && chown -R ${HOST_UID}:${HOST_GID} /opt/venv

USER runner
WORKDIR /workspace

RUN git config --global user.email "runner@atelier.local" && \
    git config --global user.name "atelier runner" && \
    git config --global --add safe.directory '*'
