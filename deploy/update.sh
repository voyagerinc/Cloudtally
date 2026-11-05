#!/usr/bin/env bash
# Pulls Tally Cloud from GitHub and restarts it, when a super admin asks for it on the System admin tab.
#
# Started by systemd (tallycloud-update.path -> tallycloud-update.service) as root. The web app itself
# runs without root rights: all it can do is drop a request file naming one fixed action
#   check    look on GitHub for new commits
#   update   pull, install Python packages, update the database schema, restart; back to the old
#            version if the new one does not start
#   restart  restart the app only
# Progress goes to $STATE/status.json and $STATE/update.log, which the app shows.
set -uo pipefail

BASE=/var/lib/tallycloud-update
INBOX=$BASE/inbox
STATE=$BASE/state
APP_USER=tallycloud
APP_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
SERVICE=/etc/systemd/system/tallycloud.service

# Everything is inside main(): bash has then read the whole script before `git merge` may replace it.
main() {
  local action started
  action=$(head -c 20 "$INBOX/request" 2>/dev/null | tr -cd 'a-z')
  rm -f "$INBOX/request"
  case "$action" in check|update|restart) ;; *) exit 0;; esac
  started=$(date -Is)

  exec >"$STATE/update.log" 2>&1
  chgrp "$APP_USER" "$STATE/update.log"; chmod 640 "$STATE/update.log"
  cd "$APP_DIR" || exit 1

  write_status() {   # state message
    local behind pending
    behind=$(git rev-list --count HEAD..@{u} 2>/dev/null)
    pending=$(git log --format='%h  %s' HEAD..@{u} 2>/dev/null | head -30)
    python3 - "$1" "$2" "$action" "$started" "$(date -Is)" "$(git rev-parse --short HEAD)" \
      "$(git log -1 --format=%s)" "$(git log -1 --format=%cI)" "$behind" "$pending" \
      >"$STATE/status.json.tmp" <<'PY'
import json, sys
state, message, action, started, now, commit, subject, committed, behind, pending = sys.argv[1:]
print(json.dumps({
    "state": state, "message": message, "action": action, "started_at": started,
    "finished_at": None if state == "running" else now,
    "commit": commit, "subject": subject, "committed_at": committed,
    "behind": int(behind) if behind.isdigit() else None,
    "pending": [line for line in pending.splitlines() if line],
}))
PY
    chgrp "$APP_USER" "$STATE/status.json.tmp"; chmod 640 "$STATE/status.json.tmp"
    mv -f "$STATE/status.json.tmp" "$STATE/status.json"
  }
  fail() { echo "!! $1"; write_status failed "$1"; exit 1; }
  healthy() {
    local port i
    port=$(grep -o -- '--port [0-9]*' "$SERVICE" | cut -d' ' -f2)
    for i in $(seq 1 30); do
      curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0
      sleep 1
    done
    return 1
  }
  install_and_migrate() {
    echo "==> Python packages"
    "$APP_DIR/.venv/bin/pip" install -q -r backend/requirements.txt || return 1
    chown -R root:"$APP_USER" "$APP_DIR"
    chmod -R g+rX,o-rwx "$APP_DIR"
    echo "==> Database schema"
    (cd backend && sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m app.cli init-db) || return 1
  }

  echo "$(date -Is)  $action requested from the web app"
  write_status running "Working: $action"

  if [ "$action" != restart ]; then
    echo "==> Looking on GitHub"
    git fetch --quiet origin || fail "Could not reach GitHub (git fetch failed; see the log)"
  fi
  if [ "$action" = check ]; then
    n=$(git rev-list --count HEAD..@{u})
    echo "$n new commit(s) on GitHub"
    write_status ok "$( [ "$n" = 0 ] && echo "Up to date" || echo "$n update(s) available")"
    exit 0
  fi

  if [ "$action" = update ]; then
    old=$(git rev-parse HEAD)
    if [ "$old" = "$(git rev-parse @{u})" ]; then
      echo "Already up to date; restarting anyway."
    else
      echo "==> Pulling $(git rev-list --count HEAD..@{u}) commit(s)"
      git merge --ff-only @{u} || fail "The server copy has local changes, so it was not updated. Run 'git status' in $APP_DIR."
      install_and_migrate || fail "Installing the new version failed (see the log). The app still runs the old version until it restarts."
    fi
  fi

  echo "==> Restarting the app"
  systemctl restart tallycloud || fail "Restart failed (see the log)"
  if healthy; then
    echo "==> The app is running"
    write_status ok "$( [ "$action" = update ] && echo "Updated and restarted" || echo "Restarted")"
    exit 0
  fi

  journalctl -u tallycloud -n 40 --no-pager
  if [ "$action" = update ] && [ "$old" != "$(git rev-parse HEAD)" ]; then
    echo "!! The new version did not start. Going back to $(git rev-parse --short "$old")."
    git reset --hard --quiet "$old"
    install_and_migrate
    systemctl restart tallycloud
    healthy && fail "The new version did not start, so the server went back to the previous version. See the log."
  fi
  fail "The app did not start after the restart. See the log, or run: journalctl -u tallycloud -n 50"
}

main "$@"
