#!/usr/bin/env bash
# Starts novelhog inside the Home Assistant add-on container: reads the add-on options, migrates the
# database, then runs the web and worker roles side by side until stopped or until one of them dies.
set -euo pipefail

OPTIONS_FILE="${OPTIONS_FILE:-/data/options.json}"

# Covers and exports always live under /data (the add-on's persistent directory).
export NOVELHOG_DATA_DIR=/data

# export_option NAME FILTER: sets NAME from the jq FILTER applied to the options file, unless the
# result is empty, so an unset option leaves the application's own default in force.
export_option() {
    local name="$1" filter="$2" value
    value="$(jq -r "$filter" "$OPTIONS_FILE")"
    if [[ -n "$value" ]]; then
        export "$name=$value"
    fi
}

# A missing file means "all defaults", so the image also runs outside Home Assistant.
if [[ -f "$OPTIONS_FILE" ]]; then
    if ! jq -e 'type == "object"' "$OPTIONS_FILE" > /dev/null 2>&1; then
        echo "novelhog add-on: $OPTIONS_FILE is not a JSON object" >&2
        exit 1
    fi
    export_option NOVELHOG_PUBLIC_URL '.public_url // empty'
    export_option NOVELHOG_VAPID_SUBJECT '.vapid_subject // empty'
    export_option NOVELHOG_ALLOWED_HOSTS '(.allowed_hosts // []) | join(",")'
    export_option NOVELHOG_FLARESOLVERR_URL '.flaresolverr_url // empty'
    export_option NOVELHOG_DATABASE_URL '.database_url // empty'
fi

# Nothing else is running yet, so a failed migration just ends the script with its status.
novelhog db migrate

PIDS=()
STOPPING=0

# shellcheck disable=SC2317,SC2329  # called by the trap below
stop() {
    STOPPING=1
    kill -TERM "${PIDS[@]}" 2> /dev/null || true
}
trap stop TERM INT

novelhog web &
PIDS+=("$!")
novelhog worker &
PIDS+=("$!")

# Returns when a child ends, and also when a trapped signal arrives, so STOPPING decides which it was.
status=0
wait -n "${PIDS[@]}" || status=$?

if ((STOPPING)); then
    wait || true
    exit 0
fi

# A child ended on its own: stop the other one and report failure, so the Supervisor restarts the add-on.
kill -TERM "${PIDS[@]}" 2> /dev/null || true
wait || true
exit "$((status != 0 ? status : 1))"
