#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
python="$repo_root/.venv/bin/python"
development_env="$repo_root/.env.development.local"
vanilla_checkout="$repo_root/.development/hermes-agent"

if [[ ! -x "$python" ]]; then
  echo "Missing $python; create the project virtualenv first." >&2
  exit 1
fi

echo "Testing against the vanilla Hermes installed in .venv"
if [[ ! -d "$vanilla_checkout/.git" ]]; then
  echo "Missing $vanilla_checkout; run scripts/bootstrap_tests.sh first." >&2
  exit 1
fi
vanilla_origin="$(git -C "$vanilla_checkout" remote get-url origin)"
if [[ "$vanilla_origin" != "https://github.com/NousResearch/hermes-agent.git" ]]; then
  echo "Vanilla Hermes origin must be https://github.com/NousResearch/hermes-agent.git; got $vanilla_origin" >&2
  exit 1
fi
env -u PYTHONPATH "$python" - <<'PY'
from pathlib import Path

import gateway

repo = Path.cwd().resolve()
expected = (repo / ".development" / "hermes-agent").resolve()
loaded = Path(gateway.__file__).resolve()
if not loaded.is_relative_to(expected):
    raise SystemExit(f"vanilla test lane did not resolve {expected}: {loaded}")
print(f"Vanilla Hermes: {loaded.parent}")
PY
env -u PYTHONPATH "$python" -m pytest "$@"

if [[ ! -f "$development_env" ]]; then
  exit 0
fi

set -a
# shellcheck disable=SC1090
source "$development_env"
set +a
: "${HERMES_AGENT_SOURCE:?Set HERMES_AGENT_SOURCE in .env.development.local}"

expected="$(cd "$repo_root/../hermes-agent" && pwd -P)"
configured="$(cd "$repo_root/$HERMES_AGENT_SOURCE" && pwd -P)"
if [[ "$configured" != "$expected" ]]; then
  echo "HERMES_AGENT_SOURCE must resolve to adjacent ../hermes-agent" >&2
  exit 1
fi

origin="$(git -C "$configured" remote get-url origin)"
if [[ "$origin" != "https://github.com/kortexa-ai/hermes-agent.git" ]]; then
  echo "Adjacent Hermes origin must be https://github.com/kortexa-ai/hermes-agent.git; got $origin" >&2
  exit 1
fi

branch="$(git -C "$configured" branch --show-current)"
if [[ "$branch" != "kortexa-ai/main" ]]; then
  echo "Adjacent Hermes must have kortexa-ai/main checked out; got ${branch:-detached HEAD}" >&2
  exit 1
fi

echo "Testing optimized pipeline against $origin ($branch)"
PYTHONPATH="$configured" "$python" -m pytest -m "kortexa_hermes or not kortexa_hermes" "$@"
