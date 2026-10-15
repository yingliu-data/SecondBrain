#!/usr/bin/env bash
# Run a SecondBrain branch with the harness in Firecracker microVMs, next to
# prod, and expose it to the iOS app.
#
#   scripts/test-branch-firecracker.sh <branch> [tenants.json]   prepare VM image + base snapshot, then run
#   scripts/test-branch-firecracker.sh --down      stop a leftover test container
#
# What it adds on top of scripts/test-branch.sh (which it then runs with
# SANDBOX=firecracker - same container, port, data dir, smoke test and tunnel):
#   1. guest image  `make image && make check` in the worktree's harness/image
#                   -> harness/image/out (vmlinux, rootfs.ext4, workspace-template.img,
#                   IMAGE_VERSION). Skipped when harness/{image,guest,protocol} are
#                   unchanged since the last build (stamp: out/.built-from).
#   2. base snapshot  `python -m app.host.sandbox.base_snapshot build-base` in a
#                   one-off container of the branch image (same Firecracker binary
#                   as the server), into ~/data/SecondBrain-test/vm/base. Skipped
#                   when `check-base` says the existing one matches the image.
# Every session then restores a microVM from that snapshot (the smoke test
# prints `vm: FirecrackerSandbox restore from base ...`).
#
# Env overrides: those of scripts/test-branch.sh (PORT, FRESH, SMOKE, ...), plus
#   REBUILD_IMAGE=1           force `make image` and a new base snapshot
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WT="$(dirname "$REPO")/SecondBrain-test"
IMAGE=secondbrain-agent-api:test
VM_CACHE="$HOME/data/SecondBrain-test/vm"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "--down" ]]; then exec "$REPO/scripts/test-branch.sh" --down; fi
BRANCH="${1:-}"
[[ -n "$BRANCH" && "$BRANCH" != -* ]] || { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

[[ -z "${2:-}" || -f "$2" ]] || die "tenants file not found: $2"
[[ -c /dev/kvm ]] || die "no /dev/kvm on this machine (KVM needed for Firecracker)"   # the container runs as root: no kvm group needed
for c in docker make python3 curl; do command -v "$c" >/dev/null || die "$c not found"; done

# ── 1. Worktree at origin/<branch> ───────────────────────────
log "Fetching origin/$BRANCH"
git -C "$REPO" fetch -q origin "$BRANCH" || die "branch '$BRANCH' not found on origin"
# Resolve here: FETCH_HEAD is per worktree, so the test worktree cannot see it.
# Checkout (not remove + re-add) keeps ignored build outputs like harness/image/out.
REV="$(git -C "$REPO" rev-parse FETCH_HEAD)"
if [[ -d "$WT" ]]; then
  git -C "$WT" checkout -q --detach "$REV" \
    || die "could not check out $REV in $WT (local changes there? git -C $WT status)"
else
  git -C "$REPO" worktree add -q --detach "$WT" "$REV"
fi
[[ -f "$WT/harness/image/Makefile" ]] || die "branch '$BRANCH' has no harness/image (not a harness branch)"
log "Worktree $WT @ $(git -C "$WT" rev-parse --short HEAD)"

# ── 2. Guest VM image ───────────────────────────────────────
OUT="$WT/harness/image/out"
STAMP="$(git -C "$WT" rev-parse HEAD:harness/image HEAD:harness/guest HEAD:harness/protocol | tr '\n' ' ')"
if [[ "${REBUILD_IMAGE:-0}" != 1 && -f "$OUT/rootfs.ext4" && "$(cat "$OUT/.built-from" 2>/dev/null)" == "$STAMP" ]]; then
  log "Guest image up to date ($(cat "$OUT/IMAGE_VERSION"))"
else
  log "Building the guest image (make image && make check) - a few minutes the first time"
  ILOG="$(mktemp)"
  if ! (cd "$WT/harness/image" && make image && make check) >"$ILOG" 2>&1; then
    tail -30 "$ILOG"; die "guest image build failed (full log: $ILOG)"
  fi
  grep -E '^\[check\] (IMAGE_VERSION|vmlinux sha256|python3 out/guest.pyz)' "$ILOG" | sed 's/^/    /' || true
  echo "$STAMP" >"$OUT/.built-from"
fi

# ── 3. agent-api image (test-branch.sh rebuilds it from cache) ──
log "Building $IMAGE"
DOCKER_BUILDKIT=1 docker build -q -t "$IMAGE" -f "$WT/agent-api/Dockerfile" "$WT" >/dev/null

# ── 4. Base snapshot, built where it runs ───────────────────
mkdir -p "$VM_CACHE"
fc_run() {
  docker run --rm --device /dev/kvm:/dev/kvm \
    -v "$OUT:/opt/harness-image:ro" \
    -v "$VM_CACHE:/var/lib/secondbrain/vm" \
    -e API_SECRET_KEY=unused \
    -e HARNESS_SANDBOX=firecracker \
    -e HARNESS_KERNEL=/opt/harness-image/vmlinux \
    -e HARNESS_ROOTFS=/opt/harness-image/rootfs.ext4 \
    -e HARNESS_WORKSPACE_TEMPLATE=/opt/harness-image/workspace-template.img \
    -e HARNESS_VM_CACHE=/var/lib/secondbrain/vm \
    "$IMAGE" "$@"
}
if [[ "${REBUILD_IMAGE:-0}" != 1 ]] && fc_run python -m app.host.sandbox.base_snapshot check-base >/dev/null 2>&1; then
  log "Base snapshot up to date ($VM_CACHE/base)"
else
  log "Building the base snapshot (boots one microVM, snapshots it at idle-wait)"
  fc_run python -m app.host.sandbox.base_snapshot build-base 2>&1 >/dev/null \
    | grep -E '^\[snapshot\] (booted|base snapshot written|error)' | sed 's/^/    /' || true
  fc_run python -m app.host.sandbox.base_snapshot check-base >/dev/null 2>&1 \
    || die "base snapshot check failed; retry with: REBUILD_IMAGE=1 $0 $BRANCH (RUNBOOK-M3 section 1)"
fi

# ── 5. Run it: container + smoke test + tunnel ──────────────
exec env SANDBOX=firecracker "$REPO/scripts/test-branch.sh" "$BRANCH" ${2:+"$2"}
