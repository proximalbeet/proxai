#!/bin/bash
# Build the ProxAI sandbox image (Hermes tool container + Claude Code).
# Usage: sandbox/build-image.sh [claude-code-version]
# To update Claude Code: rebuild with a new version, change terminal.docker_image
# in config.yaml to the new tag, then `docker rm -f` the old hermes-* container.
set -euo pipefail
VERSION="${1:-2.1.302}"
HERE="$(cd "$(dirname "$0")" && pwd)"
docker build --pull=false --build-arg "CLAUDE_CODE_VERSION=$VERSION" \
  -t "proxai-sandbox:$VERSION" "$HERE/image"
echo "built proxai-sandbox:$VERSION"
