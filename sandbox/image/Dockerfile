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
