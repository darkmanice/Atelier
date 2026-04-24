#!/usr/bin/env bash
# One-shot bootstrap: detects host UID/GID/docker-GID, generates the internal
# API token, and writes everything into .env. Safe to re-run — never
# overwrites values the user has already set.

set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".env"
TEMPLATE=".env.example"

if [[ ! -f "$TEMPLATE" ]]; then
    echo "Error: $TEMPLATE not found. Are you in the repo root?" >&2
    exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
    cp "$TEMPLATE" "$ENV_FILE"
    echo "Created $ENV_FILE from $TEMPLATE."
fi

needs_fill() {
    # True when the key is missing, empty, or still the placeholder token.
    local key="$1"
    local current
    current=$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2- || true)
    [[ -z "$current" || "$current" == "CHANGE_ME_RUN_openssl_rand_hex_32" ]]
}

set_env() {
    local key="$1" value="$2"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        # Use a delimiter unlikely to appear in values
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
    echo "  ${key}=${value}"
}

echo "Auto-detecting host values..."

if needs_fill HOST_UID; then set_env HOST_UID "$(id -u)"; fi
if needs_fill HOST_GID; then set_env HOST_GID "$(id -g)"; fi

if needs_fill DOCKER_GID; then
    dgid=$(getent group docker 2>/dev/null | cut -d: -f3 || true)
    if [[ -n "$dgid" ]]; then
        set_env DOCKER_GID "$dgid"
    else
        echo "  WARN: no 'docker' group found on this host — set DOCKER_GID manually." >&2
    fi
fi

if needs_fill INTERNAL_API_TOKEN; then
    if ! command -v openssl >/dev/null 2>&1; then
        echo "Error: openssl is required to generate INTERNAL_API_TOKEN." >&2
        exit 1
    fi
    set_env INTERNAL_API_TOKEN "$(openssl rand -hex 32)"
fi

echo ""
echo "Done. Next steps:"
echo "  1. Edit $ENV_FILE and set DEFAULT_MODEL."
echo "  2. Drop or clone your target git repos under ./projects/."
echo "  3. Run: docker compose up -d --build"
