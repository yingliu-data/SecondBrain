#!/usr/bin/env bash
# Run a SecondBrain branch next to prod and expose it to the iOS app.
#
#   scripts/test-branch.sh <branch> [tenants.json]   build + run + quick tunnel (Ctrl-C stops everything)
#   scripts/test-branch.sh --down            stop a leftover test container
#
# Prod (secondbrain-agent-api, secondbrain.yingliu.site) is never touched:
#   - the branch is checked out in a separate worktree (../SecondBrain-test)
#   - it runs as container secondbrain-test-agent-api on 127.0.0.1:$PORT
#   - it gets its own data dir: tenants.json = the given file (default: prod's), copied
#     fresh every run; skills.json seeded once from prod
#   - it shares the prod LLM (secondbrain-llm)
#   - the default tenant (every local skill, the global prompt, no MCP) uses a
#     test-only API_SECRET_KEY, generated once into $KEY_FILE (never prod's)
#   - branches with mcp-gateway/ also get a test gateway (secondbrain-test-agent-api-mcp-gateway),
#     configured from the test tenants.json, reaching wcc-mcp over agent-net
#
# Then in IndexApp -> Settings: Server URL = the printed trycloudflare URL,
# API key = the test default key (cat ~/data/SecondBrain-test/default.key). Set both back afterwards.
#
# Env overrides:
#   PORT=8001                 host port
#   SANDBOX=local             harness sandbox: local (subprocess) | firecracker (use
#                             scripts/test-branch-firecracker.sh: it builds image + base)
#   PROD_DIR=~/data/SecondBrain   where prod's agent-api/data (tenants.json) lives
#   API_SECRET_KEY=...        default-tenant key (default: the one in $KEY_FILE)
#   FRESH=1                   wipe the test data dir (sessions etc.) before starting
#   SMOKE=0                   skip the one-turn smoke test before the tunnel opens
#   NO_TUNNEL=1               stop after the smoke test (local check only, no public URL)
#   GATEWAY=0                 no mcp-gateway (no MCP tools)
#   SMOKE_MSG="..."           the smoke test's message (default: "Reply with exactly: smoke ok")
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-8001}"
SANDBOX="${SANDBOX:-local}"
PROD_DIR="${PROD_DIR:-$HOME/data/SecondBrain}"
NAME="${NAME:-secondbrain-test-agent-api}"
IMAGE=secondbrain-agent-api:test
WT="$(dirname "$REPO")/SecondBrain-test"
DATA="${DATA:-$HOME/data/SecondBrain-test/agent-api/data}"
NET_PUBLIC=secondbrain_tunnel_network    # non-internal: lets -p publish the port
NET_LLM=secondbrain_llm_internal         # internal: reaches secondbrain-llm (and agent-api <-> gateway)
NET_MCP=agent-net                        # external: the gateway reaches wcc-mcp here
GW="$NAME-mcp-gateway"                   # agent-api's name: one gateway per test server
GW_IMAGE=secondbrain-mcp-gateway:test
GW_DATA="$(dirname "$DATA")/mcp-gateway-data"   # gateway.json (regenerated each run) + gateway-state
KEY_FILE="${KEY_FILE:-$HOME/data/SecondBrain-test/default.key}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

down() {
  [[ -n "${TUNNEL_PID:-}" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
  local c
  for c in "$NAME" "$GW"; do
    if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
      log "Stopping $c"
      docker rm -f "$c" >/dev/null
    fi
  done
}

if [[ "${1:-}" == "--down" ]]; then down; exit 0; fi
BRANCH="${1:-}"
TENANTS="${2:-$PROD_DIR/agent-api/data/tenants.json}"
[[ -f "$TENANTS" ]] || die "tenants file not found: $TENANTS"
TENANTS="$(realpath "$TENANTS")"
[[ -n "$BRANCH" && "$BRANCH" != -* ]] || { sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

command -v docker >/dev/null || die "docker not found"
command -v cloudflared >/dev/null || die "cloudflared not found"
for n in "$NET_PUBLIC" "$NET_LLM"; do
  docker network inspect "$n" >/dev/null 2>&1 || die "docker network $n missing - is the prod stack up?"
done

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
SHA="$(git -C "$WT" rev-parse --short HEAD)"
log "Worktree $WT @ $SHA ($(git -C "$WT" log -1 --format=%s))"

# ── 2. Build the image ──────────────────────────────────────
# Workspace branches (harness) build from the repo root; older ones from agent-api/.
if [[ -f "$WT/pyproject.toml" ]]; then CTX="$WT"; else CTX="$WT/agent-api"; fi
log "Building $IMAGE (context: $([[ "$CTX" == "$WT" ]] && echo 'repo root' || echo agent-api/))"
DOCKER_BUILDKIT=1 docker build -q -t "$IMAGE" -f "$WT/agent-api/Dockerfile" "$CTX" >/dev/null
if [[ "${GATEWAY:-1}" == 1 && ! -f "$WT/mcp-gateway/Dockerfile" ]]; then
  log "No mcp-gateway/ on this branch: running without a gateway"; GATEWAY=0
fi
if [[ "${GATEWAY:-1}" == 1 ]]; then
  log "Building $GW_IMAGE"
  DOCKER_BUILDKIT=1 docker build -q -t "$GW_IMAGE" -f "$WT/mcp-gateway/Dockerfile" "$WT" >/dev/null
fi

# ── 3. Test data dir ────────────────────────────────────────
if [[ "${FRESH:-0}" == 1 && -d "$DATA" ]]; then
  log "FRESH=1: wiping $DATA"
  docker run --rm -v "$(dirname "$DATA"):/d" alpine rm -rf /d/data   # files are root-owned
fi
mkdir -p "$DATA"
# tenants.json: the input file, every run (edit it and restart to apply).
# The default tenant needs no entry: agent-api synthesizes it from API_SECRET_KEY.
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); [t["name"] for t in d.get("tenants", [])]' "$TENANTS" \
  || die "$TENANTS is not a valid tenants.json"
install -m 600 "$TENANTS" "$DATA/tenants.json"
log "Tenants from $TENANTS: $(python3 -c 'import json,sys; print(", ".join([t["name"] for t in json.load(open(sys.argv[1])).get("tenants", [])] + ["default"]))' "$TENANTS")"
[[ -f "$DATA/skills.json" || ! -f "$PROD_DIR/agent-api/data/skills.json" ]] \
  || cp "$PROD_DIR/agent-api/data/skills.json" "$DATA/skills.json"

# ── 4. Run ──────────────────────────────────────────────────
# API_SECRET_KEY is the default tenant's key. A test-only one, kept in
# $KEY_FILE so it survives FRESH=1 and stays the same on the phone.
mkdir -p "$(dirname "$KEY_FILE")"
OLD_KEY_FILE="$(dirname "$KEY_FILE")/general.key"        # earlier name: keep the phone's key
[[ -s "$KEY_FILE" || ! -s "$OLD_KEY_FILE" ]] || mv "$OLD_KEY_FILE" "$KEY_FILE"
[[ -s "$KEY_FILE" ]] || { (umask 077; openssl rand -hex 16 >"$KEY_FILE"); log "New default-tenant key in $KEY_FILE"; }
API_SECRET_KEY="${API_SECRET_KEY:-$(cat "$KEY_FILE")}"
down
trap 'echo; down; log "Stopped. Point IndexApp back at https://secondbrain.yingliu.site"' EXIT INT TERM

EXTRA=()
if [[ "$SANDBOX" == firecracker ]]; then
  [[ -f "$WT/harness/image/out/rootfs.ext4" ]] || die "no harness image: cd $WT/harness/image && make image"
  mkdir -p "$HOME/data/SecondBrain-test/vm"
  EXTRA+=(--device /dev/kvm:/dev/kvm
          -v "$WT/harness/image/out:/opt/harness-image:ro"
          -v "$HOME/data/SecondBrain-test/vm:/var/lib/secondbrain/vm"
          -e HARNESS_KERNEL=/opt/harness-image/vmlinux
          -e HARNESS_ROOTFS=/opt/harness-image/rootfs.ext4
          -e HARNESS_WORKSPACE_TEMPLATE=/opt/harness-image/workspace-template.img
          -e HARNESS_VM_CACHE=/var/lib/secondbrain/vm)
fi

INTERNAL_TOKEN="$(openssl rand -hex 16)"     # agent-api <-> gateway, both directions
GW_URL=""
if [[ "${GATEWAY:-1}" == 1 ]]; then
  docker network inspect "$NET_MCP" >/dev/null 2>&1 || die "docker network $NET_MCP missing (wcc-mcp's network)"
  # gateway.json from the test tenants.json (the branch's own migrate_config).
  # migrate_config grants the default tenant every upstream; drop it: only the
  # wcc tenants get MCP, each its own upstream. A tenant missing from
  # gateway.json is default-denied.
  mkdir -p "$GW_DATA"
  docker run --rm -v "$DATA/tenants.json:/in/tenants.json:ro" -e API_SECRET_KEY="$API_SECRET_KEY" \
    "$GW_IMAGE" python -m app.migrate_config /in/tenants.json \
  | python3 -c '
import json, sys
g = json.load(sys.stdin)
g["tenants"].pop("default", None)
json.dump(g, sys.stdout, indent=2)' >"$GW_DATA/gateway.json.new"
  chmod 600 "$GW_DATA/gateway.json.new"; mv "$GW_DATA/gateway.json.new" "$GW_DATA/gateway.json"

  log "Starting $GW (MCP access: $(python3 -c 'import json,sys; t=json.load(open(sys.argv[1]))["tenants"]; print(", ".join(f"{k} -> {"+".join(v["upstreams"])}" for k,v in t.items()) or "none")' "$GW_DATA/gateway.json"))"
  docker run -d --name "$GW" --network "$NET_LLM" \
    -v "$GW_DATA:/data" \
    -e AGENT_API_URL="http://$NAME:8000" \
    -e INTERNAL_TOKEN="$INTERNAL_TOKEN" \
    -e LOG_LEVEL=info \
    "$GW_IMAGE" >/dev/null
  docker network connect "$NET_MCP" "$GW"
  for i in $(seq 30); do
    docker exec "$GW" python -c 'import urllib.request; urllib.request.urlopen("http://localhost:8090/healthz", timeout=2)' 2>/dev/null && break
    [[ $i == 30 ]] && { docker logs "$GW" | tail -30; die "gateway not healthy after 30s"; }
    sleep 1
  done
  # Each upstream reachable from the gateway (TCP), so a dead wcc-mcp shows here
  # and not as a vague tool error mid-chat.
  docker exec "$GW" python -c '
import json, socket
from urllib.parse import urlsplit
for name, u in json.load(open("/data/gateway.json"))["upstreams"].items():
    p = urlsplit(u["url"])
    try:
        socket.create_connection((p.hostname, p.port or 80), timeout=3).close(); s = "reachable"
    except OSError as e:
        s = f"NOT reachable ({e})"
    print(f"    upstream {name}: {p.hostname}:{p.port} {s}")' || true
  GW_URL="http://$GW:8090"
fi

log "Starting $NAME on 127.0.0.1:$PORT (sandbox: $SANDBOX, gateway: $([[ -n "$GW_URL" ]] && echo on || echo off), no Temporal)"
docker run -d --name "$NAME" \
  --network "$NET_PUBLIC" -p "127.0.0.1:$PORT:8000" \
  -v "$DATA:/root/agent-api/data" \
  -e LLM_URL=http://secondbrain-llm:8080 \
  -e API_SECRET_KEY="$API_SECRET_KEY" \
  -e LOG_LEVEL=info \
  -e MAX_TOOL_CALLS_PER_TURN=10 \
  -e MAX_INPUT_LENGTH=4096 \
  -e RATE_LIMIT_PER_MINUTE=30 \
  -e HARNESS_SANDBOX="$SANDBOX" \
  `# absolute: the local sandbox runs the guest with cwd=/root/harness, so the default` \
  `# relative data/sessions makes the guest wait on a socket path that does not exist` \
  -e SESSIONS_ROOT=/root/agent-api/data/sessions \
  -e USERS_ROOT=/root/agent-api/data/users \
  -e MCP_GATEWAY_URL="$GW_URL" \
  -e INTERNAL_TOKEN="$INTERNAL_TOKEN" \
  "${EXTRA[@]}" "$IMAGE" >/dev/null
docker network connect "$NET_LLM" "$NAME"

log "Waiting for /health"
for i in $(seq 60); do
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  docker ps --format '{{.Names}}' | grep -qx "$NAME" || { docker logs "$NAME" | tail -40; die "container exited"; }
  sleep 1
  [[ $i == 60 ]] && { docker logs "$NAME" | tail -40; die "no /health after 60s"; }
done
echo "    $(curl -sS "http://127.0.0.1:$PORT/health")"

# ── 4b. Smoke test: one signed chat as the default tenant ────
# Its own session (smoke-<time>), so it never touches the phone's sessions.
if [[ "${SMOKE:-1}" == 1 ]]; then
  log "Smoke test: one chat turn through the harness ($SANDBOX sandbox)"
  docker exec -i -e KEY="$API_SECRET_KEY" -e MSG="${SMOKE_MSG:-Reply with exactly: smoke ok}" "$NAME" python - "smoke-$(date +%H%M%S)" <<'EOF' || die "smoke test failed (logs: docker logs $NAME)"
import glob, hashlib, hmac, json, os, sys, time, urllib.request
sid, key = sys.argv[1], os.environ["KEY"]
body = json.dumps({"session_id": sid, "message": os.environ["MSG"], "input_id": "i-1"}).encode()
ts = str(int(time.time()))
sig = hmac.new(key.encode(), ts.encode() + body, hashlib.sha256).hexdigest()
req = urllib.request.Request("http://localhost:8000/api/v1/chat", data=body, headers={
    "Authorization": f"Bearer {key}", "X-Timestamp": ts, "X-Signature": sig,
    "Content-Type": "application/json"})
t0, text, event, errors = time.monotonic(), "", "", []
with urllib.request.urlopen(req, timeout=180) as r:
    for raw in r:
        line = raw.decode().rstrip("\n")
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
            if event == "token":
                text += data.get("text", "")
            elif event == "error":
                errors.append(data)
            elif event == "done":
                break
print(f"    reply ({time.monotonic() - t0:.1f}s): {text.strip()[:600]!r}")
vm, tools = [], []
for d in glob.glob(f"/root/agent-api/data/sessions/*/*.{sid}"):
    for f in glob.glob(f"{d}/events/host-*.jsonl"):
        vm += [json.loads(l)["body"] for l in open(f) if '"type":"vm"' in l]
    for f in glob.glob(f"{d}/events/r*/*.jsonl"):
        for l in open(f):
            e = json.loads(l)
            if e.get("type") == "session_start":
                tools = e["body"].get("tools") or []
# MCP reaches the model as gateway meta-tools (search_tools / describe_tool /
# call_tool) plus any promoted upstream tools, all with side "mcp".
sides = {}
for t in tools:
    sides.setdefault(t.get("side", "?") if isinstance(t, dict) else "?", []).append(
        t.get("name") if isinstance(t, dict) else t)
print(f"    tenant default: {len(tools)} tools ("
      + ", ".join(f"{k} {len(v)}" for k, v in sorted(sides.items())) + ")")
print(f"    mcp: {', '.join(sorted(sides.get('mcp', []))) or 'none (as intended: no MCP for default)'}")
for v in vm:
    print(f"    vm: {v.get('sandbox')} {v.get('op')} from {v.get('source')} "
          f"in {v.get('ms')} ms (ready {v.get('ready_ms')} ms)")
if errors or not text.strip():
    sys.exit(f"    no reply, errors: {errors}")
EOF
fi

if [[ "${NO_TUNNEL:-0}" == 1 ]]; then log "NO_TUNNEL=1: local check done"; exit 0; fi

# ── 5. Quick tunnel ─────────────────────────────────────────
TLOG="$(mktemp)"
# An empty config: otherwise cloudflared picks up /etc/cloudflared/config.yml
# (the named pose-backend tunnel), whose catch-all `http_status:404` ingress
# rule answers every quick-tunnel request with 404.
TCFG="$(mktemp --suffix=.yml)"; echo '{}' >"$TCFG"
cloudflared tunnel --config "$TCFG" --no-autoupdate --url "http://127.0.0.1:$PORT" >"$TLOG" 2>&1 &
TUNNEL_PID=$!
URL=""
for _ in $(seq 30); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TLOG" | head -1 || true)"
  [[ -n "$URL" ]] && break
  sleep 1
done
[[ -n "$URL" ]] || { cat "$TLOG"; die "tunnel did not come up"; }

log "Checking $URL/health from the internet side"
CODE=000
for _ in $(seq 30); do
  CODE="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$URL/health" || true)"
  [[ "$CODE" == 200 ]] && break
  sleep 2
done
[[ "$CODE" == 200 ]] || { tail -20 "$TLOG"; die "public /health answered HTTP $CODE (tunnel log above)"; }

cat <<EOF

  Branch   $BRANCH @ $SHA
  Local    http://127.0.0.1:$PORT
  Public   $URL

  IndexApp -> Settings
    Server URL : $URL
    API key    : the default tenant's key:  cat $KEY_FILE

  Logs follow below (gateway: docker logs -f $GW). Ctrl-C stops everything.

EOF
docker logs -f --since 1s "$NAME"
