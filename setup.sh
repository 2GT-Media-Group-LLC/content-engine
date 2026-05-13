#!/usr/bin/env bash
# One-shot installer. Idempotent.
#   * verifies Python 3.11+
#   * creates a venv
#   * installs deps from pyproject.toml
#   * pulls the Ollama models the engine routes to
#   * scaffolds channel.yaml and .env from examples if missing
#   * initializes the SQLite DB
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "═══ content-engine setup ═══"
echo

# 1. Python version check
PYBIN="${PYTHON:-python3}"
if ! command -v "$PYBIN" >/dev/null 2>&1; then
  echo "✗ python3 not found. Install Python 3.11+ first."
  exit 1
fi
PYVER=$("$PYBIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
case "$PYVER" in
  3.11|3.12|3.13|3.14) echo "✓ python $PYVER" ;;
  *) echo "✗ python 3.11+ required, found $PYVER"; exit 1 ;;
esac

# 2. venv
if [ ! -x .venv/bin/python ]; then
  echo "→ creating venv at .venv/"
  "$PYBIN" -m venv .venv
fi
echo "✓ venv ready"

# 3. deps
echo "→ installing project + deps (this can take a few minutes the first time)"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .
echo "✓ deps installed"

# 4. Ollama check + model pulls
if ! command -v ollama >/dev/null 2>&1; then
  echo
  echo "⚠ ollama not found on PATH."
  echo "  Install from https://ollama.com/download and re-run this script."
  echo "  Continuing without model pulls."
else
  echo "→ pulling Ollama models (skipped if already present)"
  for model in nomic-embed-text gemma4:e2b gemma4:e4b qwen3:30b-a3b-instruct-2507-q8_0; do
    if ollama list 2>/dev/null | grep -q "^${model%:*}"; then
      echo "  ✓ $model (already pulled)"
    else
      echo "  → pulling $model …"
      ollama pull "$model" || echo "  ⚠ $model pull failed; continue manually with: ollama pull $model"
    fi
  done
fi

# 5. config scaffolding
if [ ! -f channel.yaml ]; then
  cp channel.example.yaml channel.yaml
  echo "✓ created channel.yaml from example — edit it for your channel"
else
  echo "✓ channel.yaml exists"
fi

if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
  else
    cat > .env <<EOF
# Optional: enables auth'd sources (YouTube via Composio).
# Get from https://app.composio.dev → Settings → API Keys
# COMPOSIO_API_KEY=ak_...
OLLAMA_HOST=http://localhost:11434
EOF
  fi
  echo "✓ created .env — drop your COMPOSIO_API_KEY in there if you want YouTube auto-collect"
else
  echo "✓ .env exists"
fi

# 6. data dirs
mkdir -p data/runs data/vectors reports

# 7. DB init + tier check
echo
echo "→ verifying engine + DB"
.venv/bin/engine init || echo "⚠ engine init failed — check above"

echo
echo "═══ setup complete ═══"
echo
echo "Next steps:"
echo "  1. $EDITOR channel.yaml          # set brand, niche, channel ID, peers"
echo "  2. .venv/bin/engine collect       # pull fresh signals"
echo "  3. .venv/bin/engine run           # generate a brief"
echo "  4. .venv/bin/engine gui           # open http://127.0.0.1:8080"
echo
echo "Optional weekly autonomous schedule + auto-start GUI (macOS):"
echo "  ./scripts/install-launchd.sh"
