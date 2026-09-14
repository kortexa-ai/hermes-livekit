#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
development_root="$repo_root/.development"
hermes_checkout="$development_root/hermes-agent"
venv="$repo_root/.venv"
expected_origin="https://github.com/NousResearch/hermes-agent.git"

mkdir -p "$development_root"
if [[ ! -e "$hermes_checkout" ]]; then
  git clone --filter=blob:none "$expected_origin" "$hermes_checkout"
elif [[ ! -d "$hermes_checkout/.git" ]]; then
  echo "$hermes_checkout exists but is not a Git checkout" >&2
  exit 1
fi

origin="$(git -C "$hermes_checkout" remote get-url origin)"
if [[ "$origin" != "$expected_origin" ]]; then
  echo "Development Hermes origin must be $expected_origin; got $origin" >&2
  exit 1
fi
if [[ -n "$(git -C "$hermes_checkout" status --porcelain)" ]]; then
  echo "Development Hermes checkout has local changes; refusing to replace them" >&2
  exit 1
fi

git -C "$hermes_checkout" fetch origin main
git -C "$hermes_checkout" switch --detach origin/main

uv venv --clear --python 3.12 "$venv"
uv pip install --python "$venv/bin/python" -e "$hermes_checkout"
uv pip install --python "$venv/bin/python" -e "$repo_root[dev]"

echo "Vanilla Hermes test environment is ready at $venv"
