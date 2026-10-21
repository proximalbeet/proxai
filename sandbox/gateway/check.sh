#!/usr/bin/env bash
# Try the gateway sandbox without touching the running gateway: runs probe.py (and a Hermes
# plugin check) as a throwaway systemd user job with the same rules as sandbox.conf.
#   sandbox/gateway/check.sh            # probe + Hermes plugin check
#   sandbox/gateway/check.sh --probe    # probe only
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
conf="$here/sandbox.conf"
runtime="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

props=()
while IFS= read -r line; do
    line="${line%%#*}"
    line="$(echo "$line" | sed 's/[[:space:]]*$//')"
    [[ -z "$line" || "$line" == \[* ]] && continue
    line="${line//%h/$HOME}"
    line="${line//%t/$runtime}"
    props+=(-p "$line")
done < "$conf"

run() {
    systemd-run --user --quiet --collect --wait --pipe \
        -p "Environment=HERMES_HOME=$HOME/.hermes/profiles/proxai" \
        "${props[@]}" "$@"
}

echo "### Probe (inside the sandbox)"
status=0
run /usr/bin/python3 -I - < "$here/probe.py" || status=$?

if [[ "${1:-}" != "--probe" ]]; then
    echo
    echo "### Hermes loads its config and plugins inside the sandbox"
    run "$HOME/.hermes/hermes-agent/.hermes/bin/hermes" --profile proxai plugins list || status=$?
fi
exit $status
