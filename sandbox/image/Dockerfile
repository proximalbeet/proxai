# ProxAI sandbox image: the Hermes tool container plus Claude Code.
# Build: sandbox/build-image.sh  (tags proxai-sandbox:<CLAUDE_CODE_VERSION>)
#
# Base is the image Hermes already used, pinned by digest so a rebuild can't
# silently pull something different.
FROM nikolaik/python-nodejs:python3.11-nodejs20@sha256:8f958bdc1b4a422bfafd97cab4f69836401f616ae985d4b57a53d254f5bcb038

ARG CLAUDE_CODE_VERSION
RUN test -n "$CLAUDE_CODE_VERSION" \
 && npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && claude --version

COPY cc-run /usr/local/bin/cc-run
RUN chmod 755 /usr/local/bin/cc-run

# Marker that cc-run checks, so --dangerously-skip-permissions is only ever
# used inside this image. Updates come from rebuilding, never from inside.
ENV PROXAI_SANDBOX=1 \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# Phase 15 (ProxAI apps): Java 17 + Android build-tools 35 + android-35 platform, copied from
# the host's ~/Android by build-image.sh (no download). Used by proxai-app-build to turn a
# generated app into an APK inside the sandbox. Read-only for the container user.
COPY android/jdk17 /opt/jdk17
COPY android/build-tools /opt/android-sdk/build-tools/35.0.0
COPY android/platform /opt/android-sdk/platforms/android-35
ENV JAVA_HOME=/opt/jdk17 \
    ANDROID_HOME=/opt/android-sdk \
    PATH=/opt/jdk17/bin:/opt/android-sdk/build-tools/35.0.0:$PATH
RUN java -version && aapt2 version && apksigner --version && command -v zipalign

# Phase 15: the ProxAI app shell (ProxMobile `appshell`, compiled code only) + the builder that
# packs a generated app into it and signs it. Staged by build-image.sh from PROXAI_SHELL_KIT_SRC.
COPY shell-kit /opt/proxai-shell
COPY proxai-app-build /usr/local/bin/proxai-app-build
RUN chmod 755 /usr/local/bin/proxai-app-build && test -s /opt/proxai-shell/classes.dex
