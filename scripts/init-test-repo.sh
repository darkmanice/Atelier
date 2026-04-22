#!/usr/bin/env bash
# Crea un repo git de prueba en ./repos/target-repo para probar el pipeline.
# Idempotente: si ya existe, lo recrea.

set -euo pipefail
cd "$(dirname "$0")/.."

REPO_DIR="repos/target-repo"

if [ -d "$REPO_DIR" ]; then
    echo "==> $REPO_DIR already exists, removing..."
    rm -rf "$REPO_DIR"
fi

mkdir -p "$REPO_DIR"
cd "$REPO_DIR"

git init -q -b main
git config user.email "test@pipeline-ia.local"
git config user.name "test"

cat > README.md <<'EOF'
# target-repo

Tiny Flask app used as a target for the pipeline-ia agents.
EOF

cat > app.py <<'EOF'
from flask import Flask

app = Flask(__name__)


@app.get("/")
def index():
    return {"service": "target-repo"}


if __name__ == "__main__":
    app.run(debug=True)
EOF

cat > requirements.txt <<'EOF'
flask==3.0.3
pytest==8.3.3
EOF

cat > .gitignore <<'EOF'
__pycache__/
*.pyc
.pytest_cache/
.venv/
EOF

mkdir -p tests
touch tests/__init__.py

git add .
git commit -q -m "initial commit"

cd - > /dev/null
echo "==> Test repo ready at $REPO_DIR"
echo "    Branch: main"
echo "    You can now submit the example task."
