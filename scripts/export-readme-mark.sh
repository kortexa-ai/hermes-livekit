#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source_png="$repo_root/docs/assets/hermes-gateway-master.png"
output_png="$repo_root/docs/assets/hermes-gateway.png"

sips -s format png -z 640 640 "$source_png" --out "$output_png" >/dev/null
echo "Exported docs/assets/hermes-gateway.png"
