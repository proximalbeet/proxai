#!/bin/bash
# Build the ProxAI sandbox image (Hermes tool container + Claude Code + Android build tools).
# Usage: sandbox/build-image.sh [claude-code-version]
# To update Claude Code: rebuild with a new version, change terminal.docker_image
# in config.yaml to the new tag, then `docker rm -f` the old hermes-* container.
#
# Phase 15 (ProxAI apps): the image also carries Java 17, Android build-tools 35 and the
# android-35 platform, so Claude Code can turn generated apps into APKs inside the sandbox.
# They are copied from the host's ~/Android (ANDROID_DIR), not downloaded, into a staged
# build context, plus the ProxAI app shell kit and proxai-app-build. Tag: proxai-sandbox:<version>-apps.
set -euo pipefail
VERSION="${1:-2.1.302}"
TAG="proxai-sandbox:${VERSION}-apps"
HERE="$(cd "$(dirname "$0")" && pwd)"
A="${ANDROID_DIR:-$HOME/Android}"
# The app shell's compiled code: `./gradlew :appshell:shellKit` in ProxMobile.
KIT="${PROXAI_SHELL_KIT_SRC:-$HOME/dev/projects/ProxMobile/appshell/build/shell-kit}"
for need in "$KIT/classes.dex" "$A/jdk17/bin/java" "$A/Sdk/build-tools/35.0.0/aapt2" "$A/Sdk/platforms/android-35/android.jar"; do
  [ -e "$need" ] || { echo "missing $need (set ANDROID_DIR)" >&2; exit 1; }
done

CTX="$(mktemp -d)"
trap 'rm -rf "$CTX"' EXIT
cp -a "$HERE/image/." "$CTX/"
mkdir -p "$CTX/android"
cp -a "$A/jdk17" "$CTX/android/jdk17"
cp -a "$A/Sdk/build-tools/35.0.0" "$CTX/android/build-tools"
cp -a "$A/Sdk/platforms/android-35" "$CTX/android/platform"
cp -a "$KIT" "$CTX/shell-kit"
mkdir -p "$CTX/shell-kit/example" && cp -a "$HERE/apps-example/proxai-test" "$CTX/shell-kit/example/"

# Two CPUs at most, so a rebuild doesn't heat the PC (thermal guard, Phase 14).
docker build --pull=false --cpuset-cpus 0-1 --build-arg "CLAUDE_CODE_VERSION=$VERSION" -t "$TAG" "$CTX"
echo "built $TAG"
