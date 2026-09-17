#!/usr/bin/env bash
# PW-1 — nightly off-box backup of all durable SecondBrain state.
#
# WHY THIS IS TASK ONE: every other failure in this system is recoverable by
# redeploying an image. This one is not. data/sessions, data/users and
# data/memory are the only artefacts that cannot be rebuilt from git — lose the
# disk and every conversation, every remembered fact and every user profile is
# gone permanently. Nothing else in WORK_PLAN.md protects against that.
#
# AN UNTESTED BACKUP IS NOT A BACKUP. Run `--verify` at least once before you
# believe this script, and again after any change to the data layout.
#
# Install (on the server):
#   sudo cp scripts/backup.sh /usr/local/bin/secondbrain-backup
#   sudo cp scripts/secondbrain-backup.{service,timer} /etc/systemd/system/
#   sudo systemctl enable --now secondbrain-backup.timer
#
# Requires: restic (apt install restic). Set RESTIC_REPOSITORY and
# RESTIC_PASSWORD_FILE in the environment or in /etc/secondbrain-backup.env.
set -euo pipefail

DATA_DIR="${SECONDBRAIN_DATA:-$HOME/SecondBrain/agent-api/data}"
RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-}"
RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE:-/etc/secondbrain-restic.pass}"
RETENTION_DAILY="${RETENTION_DAILY:-7}"
RETENTION_WEEKLY="${RETENTION_WEEKLY:-4}"
RETENTION_MONTHLY="${RETENTION_MONTHLY:-6}"

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[$(date -Is)] $*"; }

preflight() {
  command -v restic >/dev/null || die "restic not installed (apt install restic)"
  [ -n "$RESTIC_REPOSITORY" ] || die "RESTIC_REPOSITORY unset — where should backups go? (e.g. sftp:user@host:/backups/secondbrain, or b2:bucket:path)"
  [ -r "$RESTIC_PASSWORD_FILE" ] || die "RESTIC_PASSWORD_FILE unreadable: $RESTIC_PASSWORD_FILE"
  [ -d "$DATA_DIR" ] || die "data dir not found: $DATA_DIR (set SECONDBRAIN_DATA)"
  export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE
  restic snapshots >/dev/null 2>&1 || {
    log "repository not initialised; running restic init"
    restic init
  }
}

# What we back up, and deliberately what we don't.
#   data/sessions   — conversation history + traces. IRREPLACEABLE.
#   data/users      — durable memory + profiles. IRREPLACEABLE.
#   data/tenants.json, data/skills.json — config. Small, annoying to rebuild.
#   data/security.log — excluded: rotating, high-volume, low value.
#   data/cache      — excluded: by definition regenerable.
backup() {
  preflight
  log "backing up $DATA_DIR -> $RESTIC_REPOSITORY"
  restic backup "$DATA_DIR" \
    --tag secondbrain \
    --exclude "$DATA_DIR/security.log*" \
    --exclude "$DATA_DIR/cache" \
    --exclude "**/__pycache__" \
    --exclude "**/*.tmp"
  log "pruning old snapshots"
  restic forget --tag secondbrain \
    --keep-daily "$RETENTION_DAILY" \
    --keep-weekly "$RETENTION_WEEKLY" \
    --keep-monthly "$RETENTION_MONTHLY" \
    --prune
  log "done"
}

# The step people skip. Restores the latest snapshot into a scratch dir and
# asserts the things that actually matter are present and parseable.
verify() {
  preflight
  local scratch
  scratch="$(mktemp -d)"
  trap 'rm -rf "$scratch"' EXIT
  log "restoring latest snapshot into $scratch"
  restic restore latest --target "$scratch"

  local restored="$scratch$DATA_DIR"
  [ -d "$restored" ] || die "restore produced no $restored — layout changed?"

  local sessions
  sessions="$(find "$restored/sessions" -maxdepth 1 -type d 2>/dev/null | wc -l)"
  log "restored session directories: $sessions"
  [ "$sessions" -gt 1 ] || die "no session directories restored — backup is not capturing conversations"

  # Every session.json must still be valid JSON. A backup of corrupt data is
  # worse than none, because it looks like protection.
  local bad=0
  while IFS= read -r f; do
    python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$f" 2>/dev/null || { echo "  CORRUPT: $f"; bad=$((bad+1)); }
  done < <(find "$restored/sessions" -name session.json 2>/dev/null)
  [ "$bad" -eq 0 ] || die "$bad corrupt session.json files in the restored snapshot"

  if [ -f "$restored/tenants.json" ]; then
    python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$restored/tenants.json" \
      || die "restored tenants.json is not valid JSON"
    log "tenants.json restored and parseable"
  else
    log "WARNING: no tenants.json in snapshot — expected if you have not created one"
  fi

  log "VERIFY PASSED — restore is real. Re-run this after any data-layout change."
}

case "${1:-backup}" in
  backup)  backup ;;
  verify)  verify ;;
  restore) preflight; shift; restic restore "${1:-latest}" --target "${2:?usage: restore <snapshot|latest> <target-dir>}" ;;
  *) echo "usage: $0 {backup|verify|restore <snapshot> <target>}" >&2; exit 2 ;;
esac
