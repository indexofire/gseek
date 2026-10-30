#!/usr/bin/env bash
# Render the documentation diagrams from their Graphviz sources.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
src="$root/dev/diagrams"
out="$root/docs/assets/diagrams"

mkdir -p "$out"
for source in "$src"/*.dot; do
  name="$(basename "${source%.dot}")"
  dot -Tpng -Gdpi=144 "$source" -o "$out/$name.png"
  echo "rendered docs/assets/diagrams/$name.png"
done
