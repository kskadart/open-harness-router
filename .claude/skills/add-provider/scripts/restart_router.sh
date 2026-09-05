#!/bin/bash
# One-shot restart of the open-harness-router LaunchAgent with a health wait,
# a "new config is live" check and an optional rollback of routing.yaml.
#
# Usage:
#   bash restart_router.sh [--expect-provider NAME] [--routing-backup PATH]
#
#   --expect-provider NAME  success additionally requires NAME in the /health
#                           "providers" list. Use it for a NEW provider; for a
#                           model added to an existing provider omit it.
#   --routing-backup PATH   copy of routing.yaml (cp -p routing.yaml
#                           routing.yaml.bak-<TS>) to restore when the service
#                           does not come up healthy within the timeout.
#                           Without it the script reports and exits 2 instead
#                           of rolling back. Relative paths resolve against
#                           the repository root. The edited routing.yaml is
#                           first copied to routing.yaml.failed-<epoch> (the
#                           path is printed), so a rollback never loses it.
#
# The launchd label:
#   The default is the README placeholder com.example.open-harness-router,
#   which no machine actually runs: the real label is a personal identifier
#   and is not committed. Give it either in OHR_LAUNCHD_LABEL or, so that
#   zero-argument runs keep working, in a .launchd-label file at the
#   repository root -- one line with the label, for example
#   com.<you>.open-harness-router. That file is gitignored. The environment
#   variable wins over the file; a file that holds no label is an error.
#
# Environment overrides:
#   OHR_REPO              repository root (default: four levels above this script)
#   OHR_LAUNCHD_LABEL     launchd label (default: the .launchd-label file at the
#                         repository root, else com.example.open-harness-router)
#   OHR_HEALTH_URL        health endpoint (default: http://127.0.0.1:8787/health)
#   OHR_ERR_LOG           service stderr log
#                         (default: ~/Library/Logs/open-harness-router.err.log)
#   OHR_HEALTH_TIMEOUT_S  seconds to wait for a healthy restart, a positive
#                         integer (default: 40 = ThrottleInterval 10 from the
#                         plist + ~11 s startup + margin). Spent in wall-clock
#                         time, not poll iterations, so a curl that hangs for
#                         its whole -m 3 eats the budget like any other wait.
#
# Exit codes (every failure message names the cause and the next command):
#   0   healthy with the new config
#   1   rolled back to --routing-backup and healthy again on the old config
#   2   bad arguments or an empty .launchd-label, service down, or unhealthy
#       and rollback impossible (no backup given, backup missing, or still
#       unhealthy after the rollback)
#   64  this machine is not macOS. The ROUTER runs on Linux too; only this
#       helper is launchd-specific, so the message prints the ready-to-paste
#       systemd equivalent (restart + /health poll) instead of a refusal.
#   65  no service loaded under the label: either the label is still the
#       published placeholder (expected on a fresh clone) or the real label
#       is not bootstrapped. The message says which and how to fix it.
#
# Why one script: the calling Claude Code session itself talks through this
# router, so the restart must be ONE foreground command that returns only
# after /health confirms the NEW process. Right after `kickstart -k` the old
# process may still answer /health with the old config while it drains
# (README.md, "Running"), and an existing provider name alone cannot tell the
# two apart -- so success is "HTTP 200 AND one pid that both preceded and
# outlived that request AND differs from the pre-kickstart pid AND (when
# given) the expected provider is listed". Rollback happens on the timeout or
# on a startup error in err.log, and only when --routing-backup was given.
#
# Written for the stock macOS bash 3.2: no arrays, no mapfile, no `set -e`.
set -u

# The header block, from the line below the shebang to the `set -u` above.
usage() {
  sed -n '2,/^set -u$/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

# The systemd equivalent of everything this script does: restart the unit,
# poll /health for the same wall-clock budget, print the same OK/FAIL line.
# Printed, never run -- this script is the launchd path, and the unit name
# is a placeholder only the operator knows (README, "Linux (systemd)").
# `\$` keeps a variable for the pasted shell; a bare `$` expands here, which
# is how the real health URL and timeout reach the recipe.
print_systemd_recipe() {
  cat <<EOF
  UNIT=open-harness-router.service   # YOUR unit name (README, "Linux (systemd)")
  systemctl --user restart "\$UNIT" || { echo "FAIL: systemctl --user restart \$UNIT failed; read 'systemctl --user status \$UNIT'" >&2; exit 2; }
  started=\$(date +%s)
  while :; do
    code=\$(curl -sS -m 3 -o /dev/null -w '%{http_code}' $HEALTH_URL 2>/dev/null)
    waited=\$((\$(date +%s) - started))
    if [ "\$code" = "200" ]; then echo "OK: healthy with the new config after \${waited}s"; curl -sS -m 3 $HEALTH_URL; break; fi
    if [ "\$waited" -ge $HEALTH_TIMEOUT_S ]; then
      echo "FAIL: no HTTP 200 from $HEALTH_URL within ${HEALTH_TIMEOUT_S}s (last status: \${code:-none}); read 'journalctl --user -u \$UNIT -n 50', restore your routing.yaml backup, restart again" >&2
      exit 2
    fi
    sleep 1
  done
EOF
}

PLACEHOLDER_LABEL="com.example.open-harness-router"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OHR_REPO="${OHR_REPO:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"
HEALTH_URL="${OHR_HEALTH_URL:-http://127.0.0.1:8787/health}"
ERR_LOG="${OHR_ERR_LOG:-$HOME/Library/Logs/open-harness-router.err.log}"
HEALTH_TIMEOUT_S="${OHR_HEALTH_TIMEOUT_S:-40}"

EXPECT_PROVIDER=""
ROUTING_BACKUP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-provider)
      if [ $# -lt 2 ]; then
        echo "restart_router.sh: --expect-provider needs a value -- the provider name" \
          "that must appear in /health, e.g. --expect-provider corp_gateway; nothing" \
          "was restarted" >&2
        exit 2
      fi
      EXPECT_PROVIDER="$2"
      shift 2
      ;;
    --routing-backup)
      if [ $# -lt 2 ]; then
        echo "restart_router.sh: --routing-backup needs a value -- the routing.yaml" \
          "copy to restore on failure, e.g. --routing-backup routing.yaml.bak-1757000000;" \
          "nothing was restarted" >&2
        exit 2
      fi
      ROUTING_BACKUP="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "restart_router.sh: unknown argument: $1; nothing was restarted." \
        "The accepted options follow." >&2
      usage >&2
      exit 2
      ;;
  esac
done

# A non-numeric timeout would make the `[ -lt ]` in the wait loop fail, the
# loop exit at once and a healthy service be rolled back as "not healthy".
case "$HEALTH_TIMEOUT_S" in
  '' | *[!0-9]*)
    echo "restart_router.sh: OHR_HEALTH_TIMEOUT_S must be a positive integer number of" \
      "seconds, got '$HEALTH_TIMEOUT_S'; nothing was restarted. Unset the variable to" \
      "use the default 40, or export a plain number of seconds" >&2
    exit 2
    ;;
esac
if [ "$HEALTH_TIMEOUT_S" -lt 1 ]; then
  echo "restart_router.sh: OHR_HEALTH_TIMEOUT_S must be at least 1 second, got" \
    "'$HEALTH_TIMEOUT_S'; nothing was restarted. Unset the variable to use the" \
    "default 40, or export a larger number of seconds" >&2
  exit 2
fi

if [ "$(uname -s)" != "Darwin" ]; then
  {
    echo "restart_router.sh: this script drives launchd (launchctl kickstart) and this" \
      "machine runs $(uname -s), not macOS, so it cannot restart the service here."
    echo "Restart it with systemd instead -- paste this block, with YOUR unit name:"
    print_systemd_recipe
    echo "Nothing is wrong with the router: it runs on Linux just as well, only this" \
      "restart helper is launchd-specific."
  } >&2
  exit 64
fi

# Resolved only after the platform check: on Linux there is no launchd
# label to get wrong, and an "empty .launchd-label" refusal there would
# hide the systemd recipe the operator actually needs.
LABEL_FILE="$OHR_REPO/.launchd-label"
if [ -n "${OHR_LAUNCHD_LABEL:-}" ]; then
  LABEL="$OHR_LAUNCHD_LABEL"
elif [ -f "$LABEL_FILE" ]; then
  # Hand-written file: first non-blank line, whitespace stripped.
  LABEL="$(sed 's/[[:space:]]//g' "$LABEL_FILE" | sed -n '/./{p;q;}')"
  if [ -z "$LABEL" ]; then
    {
      echo "restart_router.sh: $LABEL_FILE exists but holds no launchd label, so the" \
        "service to restart is unknown; nothing was restarted."
      echo "Write the label on one line:"
      echo "  printf '%s\\n' 'com.<you>.open-harness-router' > $LABEL_FILE"
      echo "'launchctl print gui/\$(id -u) | grep -i harness' shows the label; the" \
        "OHR_LAUNCHD_LABEL variable overrides the file."
    } >&2
    exit 2
  fi
else
  LABEL="$PLACEHOLDER_LABEL"
fi

cd "$OHR_REPO" || {
  echo "restart_router.sh: cannot cd to the repository root $OHR_REPO, so nothing was" \
    "restarted; run the script from a checkout, or point OHR_REPO at the repository" \
    "root (export OHR_REPO=/path/to/open-harness-router)" >&2
  exit 2
}
SERVICE="gui/$(id -u)/$LABEL"

if ! launchctl print "$SERVICE" >/dev/null 2>&1; then
  if [ "$LABEL" = "$PLACEHOLDER_LABEL" ]; then
    {
      echo "restart_router.sh: no launchd service was found under $SERVICE, so nothing" \
        "was restarted."
      echo "That label is the placeholder this repository ships, not a real one: on a" \
        "fresh clone this is expected and nothing is broken."
      echo "Write this machine's label into the gitignored $LABEL_FILE:"
      echo "  printf '%s\\n' 'com.<you>.open-harness-router' > $LABEL_FILE"
      echo "or export it for this shell:"
      echo "  export OHR_LAUNCHD_LABEL=com.<you>.open-harness-router"
      echo "To find the label: launchctl print gui/\$(id -u) | grep -i harness"
      echo "  (or read the Label key of your plist in ~/Library/LaunchAgents/)"
    } >&2
  else
    {
      echo "restart_router.sh: label '$LABEL' resolved, but no service is bootstrapped" \
        "under $SERVICE, so nothing was restarted."
      echo "Load the service into your launchd domain, then re-run this script:"
      echo "  launchctl bootstrap gui/\$(id -u) $HOME/Library/LaunchAgents/$LABEL.plist"
      echo "If the plist lives elsewhere, give its real path;" \
        "'launchctl print gui/\$(id -u) | grep -i harness' lists what is loaded now."
    } >&2
  fi
  exit 65
fi

# `launchctl print` shows a "\tpid = N" line only while the process runs.
service_pid() {
  launchctl print "$SERVICE" 2>/dev/null | awk '/^[[:space:]]*pid = /{print $3; exit}'
}

err_log_size() {
  if [ -f "$ERR_LOG" ]; then
    wc -c < "$ERR_LOG" | tr -d ' '
  else
    echo 0
  fi
}

# True when the service already wrote a startup failure since
# ERR_LOG_SIZE_BEFORE was taken. `sys.exit("open-harness-router: ...")` covers
# the handled failures (bad settings, unreadable routing.yaml) and a Traceback
# the unhandled ones; uvicorn's own shutdown and access lines match neither.
startup_error_logged() {
  local size_now
  size_now="$(err_log_size)"
  [ "$size_now" -gt "$ERR_LOG_SIZE_BEFORE" ] || return 1
  tail -c "+$((ERR_LOG_SIZE_BEFORE + 1))" "$ERR_LOG" |
    grep -q -e '^open-harness-router: ' -e '^Traceback (most recent call last):'
}

# Prints whatever the service wrote to err.log since ERR_LOG_SIZE_BEFORE was
# taken: the `open-harness-router: ...` startup error or a Traceback.
show_new_err_log_lines() {
  local size_now
  size_now="$(err_log_size)"
  if [ "$size_now" -gt "$ERR_LOG_SIZE_BEFORE" ]; then
    echo "--- new lines in $ERR_LOG ---"
    tail -c "+$((ERR_LOG_SIZE_BEFORE + 1))" "$ERR_LOG"
    echo "--- end of err.log excerpt ---"
  else
    echo "err.log: no new lines"
  fi
}

HEALTH_BODY=""
# Sets HEALTH_BODY to the last response body; succeeds only on HTTP 200.
fetch_health() {
  local response status
  response="$(curl -sS -m 3 -w '\n%{http_code}' "$HEALTH_URL" 2>/dev/null)" || return 1
  status="${response##*$'\n'}"
  HEALTH_BODY="${response%$'\n'*}"
  [ "$status" = "200" ]
}

# $1 = pid before the kickstart ("" = unknown, pid check skipped),
# $2 = provider that must be listed ("" = none).
new_process_healthy() {
  local previous_pid="$1" required_provider="$2" pid_before pid_after
  # The pid is read on both sides of the request. The old process can answer
  # with the old config and exit while launchd already starts its successor,
  # and a single read after the response would then show a changed pid for a
  # body the old process served.
  pid_before="$(service_pid)"
  fetch_health || return 1
  pid_after="$(service_pid)"
  if [ "$pid_before" != "$pid_after" ]; then
    return 1
  fi
  if [ -n "$previous_pid" ]; then
    # An empty pid means launchd runs no process at all, so whatever answered
    # is not the service this script restarted.
    if [ -z "$pid_after" ] || [ "$pid_after" = "$previous_pid" ]; then
      return 1
    fi
  fi
  if [ -n "$required_provider" ]; then
    case "$HEALTH_BODY" in
      *"\"$required_provider\""*) ;;
      *) return 1 ;;
    esac
  fi
  return 0
}

WAITED_S=0
WAIT_FAILURE=""
# Polls new_process_healthy "$1" "$2" once a second and sets WAITED_S; on
# failure WAIT_FAILURE says why. The budget is wall clock rather than a poll
# count: each curl can block for its own -m 3 seconds, which would stretch a
# 40 s wait past two minutes when connections hang. A startup error already in
# err.log ends the wait at once -- that process will not come up, and sitting
# out the budget only lengthens the outage before the rollback.
wait_until_healthy() {
  local started_at
  started_at="$(date +%s)"
  WAITED_S=0
  WAIT_FAILURE=""
  while :; do
    if new_process_healthy "$1" "$2"; then
      WAITED_S=$(($(date +%s) - started_at))
      return 0
    fi
    WAITED_S=$(($(date +%s) - started_at))
    if startup_error_logged; then
      WAIT_FAILURE="startup error in $ERR_LOG after ${WAITED_S}s"
      return 1
    fi
    if [ "$WAITED_S" -ge "$HEALTH_TIMEOUT_S" ]; then
      WAIT_FAILURE="no healthy new process within the ${HEALTH_TIMEOUT_S}s budget"
      return 1
    fi
    sleep 1
  done
}

kickstart() {
  if ! launchctl kickstart -k "$SERVICE"; then
    {
      echo "restart_router.sh: launchctl kickstart -k $SERVICE failed, so the service" \
        "was NOT restarted and keeps running whatever it started with."
      echo "It may have been unloaded since the check above: run" \
        "'launchctl print $SERVICE', and if it is gone" \
        "'launchctl bootstrap gui/\$(id -u) $HOME/Library/LaunchAgents/$LABEL.plist'."
    } >&2
    return 1
  fi
}

OLD_PID="$(service_pid)"
ERR_LOG_SIZE_BEFORE="$(err_log_size)"
if [ -z "$OLD_PID" ]; then
  echo "WARNING: no 'pid = N' line in launchctl print output; the pid-change check is skipped"
fi

echo "restart: $SERVICE (pid ${OLD_PID:-unknown}), timeout ${HEALTH_TIMEOUT_S}s," \
  "expect provider: ${EXPECT_PROVIDER:-<none>}, backup: ${ROUTING_BACKUP:-<none>}"
kickstart || exit 2

if wait_until_healthy "$OLD_PID" "$EXPECT_PROVIDER"; then
  echo "OK: healthy with the new config after ${WAITED_S}s, pid ${OLD_PID:-unknown} -> $(service_pid)"
  echo "health: $HEALTH_BODY"
  show_new_err_log_lines
  exit 0
fi

echo "FAIL: not healthy with the new config -- $WAIT_FAILURE" \
  "(last /health response: ${HEALTH_BODY:-<none>}). The cause is in $ERR_LOG," \
  "excerpt below." >&2
show_new_err_log_lines

if [ -z "$ROUTING_BACKUP" ]; then
  {
    echo "no --routing-backup was given, so NOTHING was rolled back: the edited" \
      "routing.yaml is still live and launchd keeps restarting the service with it."
    echo "Restore a working config by hand, verify it, then restart:"
    echo "  cp -p routing.yaml.bak-<TS> routing.yaml"
    echo "  PYTHONPATH=src .venv/bin/python -m cli.validate_routing"
    echo "  bash $0"
  } >&2
  exit 2
fi
if [ ! -f "$ROUTING_BACKUP" ]; then
  {
    echo "rollback impossible: the --routing-backup file $ROUTING_BACKUP does not exist" \
      "(relative paths resolve against $OHR_REPO), so routing.yaml is untouched and the" \
      "service keeps restarting with it."
    echo "List the backups and re-run with the real path:"
    echo "  ls -1 $OHR_REPO/routing.yaml.bak-*"
  } >&2
  exit 2
fi

# The restart can fail for reasons that have nothing to do with the edit
# (a mistyped --expect-provider, a service that is simply slow), so the
# edited file is kept before the backup overwrites it.
FAILED_COPY="routing.yaml.failed-$(date +%s)"
if ! cp -p routing.yaml "$FAILED_COPY"; then
  echo "rollback aborted: could not save the current routing.yaml as" \
    "$OHR_REPO/$FAILED_COPY (no space or no write permission?); nothing was rolled" \
    "back and routing.yaml is left untouched, so no edit was lost. Free space or fix" \
    "the permissions on $OHR_REPO, then re-run the same command" >&2
  exit 2
fi
echo "saved the config that did not come up as $OHR_REPO/$FAILED_COPY"

echo "rolling back: cp -p $ROUTING_BACKUP routing.yaml"
if ! cp -p "$ROUTING_BACKUP" routing.yaml; then
  echo "rollback failed: could not copy $ROUTING_BACKUP over routing.yaml, so the" \
    "configuration that did not come up is STILL live (a copy of it is saved as" \
    "$OHR_REPO/$FAILED_COPY). Restore it by hand --" \
    "'cp -p $ROUTING_BACKUP routing.yaml' -- and re-run this script" >&2
  exit 2
fi
PID_BEFORE_ROLLBACK="$(service_pid)"
ERR_LOG_SIZE_BEFORE="$(err_log_size)"
kickstart || exit 2

if wait_until_healthy "$PID_BEFORE_ROLLBACK" ""; then
  echo "ROLLED BACK: healthy again after ${WAITED_S}s on the PREVIOUS configuration" \
    "from $ROUTING_BACKUP, pid -> $(service_pid): routing.yaml holds that previous" \
    "configuration and the router is serving it."
  echo "The rejected configuration was saved as $OHR_REPO/$FAILED_COPY -- fix it there," \
    "check it with 'PYTHONPATH=src .venv/bin/python -m cli.validate_routing', copy it" \
    "back over routing.yaml and re-run this script."
  echo "health: $HEALTH_BODY"
  show_new_err_log_lines
  exit 1
fi

echo "DOWN: still unhealthy after the rollback -- $WAIT_FAILURE. routing.yaml holds" \
  "the previous configuration from $ROUTING_BACKUP again (the rejected one is saved as" \
  "$OHR_REPO/$FAILED_COPY), so the cause is most likely not routing.yaml: read" \
  "$ERR_LOG (excerpt below) and 'launchctl print $SERVICE'." >&2
show_new_err_log_lines
exit 2
