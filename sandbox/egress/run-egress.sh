#!/bin/bash
# Run the ProxAI egress proxy container in the foreground (systemd: proxai-egress.service).
#
#   proxai-sandbox  --internal network, no route anywhere. The Hermes sandbox joins only this.
#   proxai-uplink   normal network. Only this proxy joins both, so it's the only way out.
set -euo pipefail

STATE="$HOME/.hermes/profiles/proxai/egress"
BIN="$HOME/.hermes/tools/iron-proxy-0.39.0-linux-x64/iron-proxy"
# Same pinned base as the sandbox image (already on disk; has CA roots for upstream TLS).
IMAGE="nikolaik/python-nodejs:python3.11-nodejs20@sha256:8f958bdc1b4a422bfafd97cab4f69836401f616ae985d4b57a53d254f5bcb038"
NAME=proxai-egress

for f in "$BIN" "$STATE/proxy.yaml" "$STATE/ca.crt" "$STATE/ca.key" "$STATE/claude-oauth.env"; do
  [ -e "$f" ] || { echo "missing $f (run sandbox/egress/setup-egress.py)" >&2; exit 1; }
done

docker network inspect proxai-sandbox >/dev/null 2>&1 || docker network create --internal proxai-sandbox >/dev/null
docker network inspect proxai-uplink  >/dev/null 2>&1 || docker network create proxai-uplink >/dev/null
docker rm -f "$NAME" >/dev/null 2>&1 || true

exec docker run --rm --name "$NAME" \
  --network proxai-uplink --network proxai-sandbox \
  --user "$(id -u):$(id -g)" --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 256m --pids-limit 128 \
  --env-file "$STATE/claude-oauth.env" \
  -v "$BIN:/usr/local/bin/iron-proxy:ro" \
  -v "$STATE/proxy.yaml:/etc/iron-proxy/proxy.yaml:ro" \
  -v "$STATE/ca.crt:/etc/iron-proxy/ca.crt:ro" \
  -v "$STATE/ca.key:/etc/iron-proxy/ca.key:ro" \
  --entrypoint /usr/local/bin/iron-proxy \
  "$IMAGE" -config /etc/iron-proxy/proxy.yaml
