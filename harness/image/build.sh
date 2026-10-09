#!/usr/bin/env bash
# harness/image/build.sh -- build the guest VM image (harness/docs/IMAGE.md).
#
#   build.sh pyz        harness/guest + harness/protocol -> out/guest.pyz   (stdlib zipapp)
#   build.sh rootfs     Dockerfile.rootfs -> docker export -> out/rootfs.ext4 (mkfs.ext4 -d)
#   build.sh kernel     pinned Firecracker CI kernel -> out/vmlinux (sha256 verified)
#   build.sh workspace  out/workspace-template.img (sparse ext4; HARNESS_WORKSPACE_TEMPLATE)
#   build.sh firecracker  pinned release tarball -> out/firecracker (host binary, sha256 verified)
#   build.sh version    out/IMAGE_VERSION = sha256("<kernel sha>\n<rootfs sha>\n")[:16]
#   build.sh all        pyz rootfs kernel workspace version
#   build.sh snapshot   base snapshot into $HARNESS_VM_CACHE/base (needs /dev/kvm; build_snapshot.py)
#   build.sh check      sizes, pyz runs, rootfs contains the init + pyz
#   build.sh clean      remove out/ and .build/
#
# Idempotent: every step skips work whose output is newer than its inputs
# (or whose checksum already matches). No sudo. Needs: python3 >= 3.12, docker
# (for rootfs), curl, sha256sum; mkfs.ext4 from e2fsprogs on the host OR docker
# (MKFS_MODE=auto|host|docker, see rootfs_mkfs below).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="$(cd "$HERE/.." && pwd)"              # harness/ (guest/, protocol/)
OUT="${OUT:-$HERE/out}"
BUILD="${BUILD:-$HERE/.build}"
MKFS_MODE="${MKFS_MODE:-auto}"
ROOTFS_IMAGE_TAG="${ROOTFS_IMAGE_TAG:-secondbrain/harness-rootfs:local}"
PYTHON="${PYTHON:-python3}"
# shellcheck source=versions.env
source "$HERE/versions.env"

log()  { printf '[image] %s\n' "$*" >&2; }
die()  { printf '[image] error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing tool: $1"; }
mkdir -p "$OUT" "$BUILD"

sha_ok() {  # sha_ok FILE SHA256
    [ -f "$1" ] || return 1
    local got; got="$(sha256sum "$1" | cut -d' ' -f1)"
    [ "$got" = "$2" ]
}

fetch_verified() {  # fetch_verified URL SHA256 DEST
    local url="$1" sha="$2" dest="$3"
    if sha_ok "$dest" "$sha"; then log "$(basename "$dest"): sha256 matches, kept"; return 0; fi
    need curl
    log "downloading $url"
    curl -fL --retry 3 --progress-bar -o "$dest.part" "$url"
    sha_ok "$dest.part" "$sha" || { rm -f "$dest.part"; die "sha256 mismatch for $url"; }
    mv "$dest.part" "$dest"
}

newest_mtime() {  # newest_mtime PATH...  -> epoch seconds of the newest file under the paths
    find "$@" -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -printf '%T@\n' 2>/dev/null \
        | sort -n | tail -1 | cut -d. -f1
}

up_to_date() {  # up_to_date OUTPUT INPUT_PATH...  -> 0 if OUTPUT is newer than every input
    local out="$1"; shift
    [ -f "$out" ] || return 1
    local o i; o="$(stat -c %Y "$out")"; i="$(newest_mtime "$@")"
    [ -n "$i" ] && [ "$o" -ge "$i" ]
}

# ---------------------------------------------------------------- pyz
build_pyz() {
    local pyz="$OUT/guest.pyz"
    if up_to_date "$pyz" "$HARNESS/guest" "$HARNESS/protocol" "$HERE/build.sh"; then
        log "guest.pyz up to date"; return 0
    fi
    need "$PYTHON"
    local stage="$BUILD/pyz"
    rm -rf "$stage"; mkdir -p "$stage"
    for pkg in guest protocol; do
        [ -d "$HARNESS/$pkg" ] || die "missing package $HARNESS/$pkg"
        cp -r "$HARNESS/$pkg" "$stage/$pkg"
    done
    find "$stage" -name '__pycache__' -type d -prune -exec rm -rf {} +
    find "$stage" -name 'test_*.py' -delete
    cat > "$stage/__main__.py" <<'PY'
"""guest.pyz entry point: runs guest.main.main() (harness/docs/GUEST.md)."""
import sys
from guest import main as _main_module

if hasattr(_main_module, "main"):
    sys.exit(_main_module.main())
# Scaffolding: guest.main may still be a stub. Fail loudly but import-clean.
sys.stderr.write("guest.pyz: guest.main has no main() yet (stub build)\n")
sys.exit(3)
PY
    "$PYTHON" -m zipapp "$stage" -o "$pyz.tmp" -p "/usr/bin/env python3" --compress
    mv "$pyz.tmp" "$pyz"
    "$PYTHON" -c "import sys; sys.path.insert(0, '$pyz'); import guest, protocol, guest.main; print('guest.pyz imports ok')" >&2
    log "built $pyz ($(stat -c %s "$pyz") bytes)"
}

# ------------------------------------------------------------- rootfs
rootfs_size_mib() {  # rootfs_size_mib DIR
    if [ "${ROOTFS_MIB:-0}" -gt 0 ]; then echo "$ROOTFS_MIB"; return; fi
    local used; used="$(du -sm "$1" | cut -f1)"
    local size=$(( used + used / 4 + 64 ))
    [ "$size" -lt 256 ] && size=256
    echo "$size"
}

rootfs_mkfs() {  # rootfs_mkfs TAR IMAGE   (extract + populate, no privileges)
    local tar="$1" img="$2" mode="$MKFS_MODE"
    if [ "$mode" = auto ]; then
        if command -v docker >/dev/null 2>&1; then mode=docker
        elif command -v mkfs.ext4 >/dev/null 2>&1; then mode=host
        else die "need docker or mkfs.ext4 (e2fsprogs) to build the rootfs"; fi
    fi
    case "$mode" in
    docker)
        # Root inside the build container, so every file in the image is owned
        # by root and mkfs.ext4 -d records that. Nothing privileged: it writes a file.
        log "populating rootfs.ext4 inside a build container (mkfs.ext4 -d)"
        docker run --rm \
            -v "$(dirname "$tar"):/in:ro" -v "$(dirname "$img"):/out" \
            -e ROOTFS_MIB="${ROOTFS_MIB:-0}" -e UIDGID="$(id -u):$(id -g)" \
            -e TAR="/in/$(basename "$tar")" -e IMG="/out/$(basename "$img")" \
            "$ROOTFS_BASE_IMAGE" bash -euc '
                apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends e2fsprogs >/dev/null
                mkdir /r && tar -xpf "$TAR" -C /r
                used=$(du -sm /r | cut -f1); size=$ROOTFS_MIB
                if [ "$size" -le 0 ]; then size=$(( used + used / 4 + 64 )); [ "$size" -lt 256 ] && size=256; fi
                rm -f "$IMG"
                mkfs.ext4 -F -q -L rootfs -d /r -E root_owner=0:0 "$IMG" "${size}M"
                chown "$UIDGID" "$IMG"
                echo "[image] rootfs.ext4: ${size} MiB (tree ${used} MiB)" >&2'
        ;;
    host)
        need mkfs.ext4
        local tree="$BUILD/rootfs"
        rm -rf "$tree"; mkdir -p "$tree"
        tar -xpf "$tar" -C "$tree" 2>/dev/null || tar -xf "$tar" -C "$tree"
        local size; size="$(rootfs_size_mib "$tree")"
        rm -f "$img"
        if command -v fakeroot >/dev/null 2>&1; then
            # fakeroot keeps uid 0 ownership for mkfs -d; without it every file is owned
            # by the build user (harmless: the guest runs as root and reads everything).
            fakeroot mkfs.ext4 -F -q -L rootfs -d "$tree" "$img" "${size}M"
        else
            log "note: no fakeroot; files in the image will be owned by uid $(id -u)"
            mkfs.ext4 -F -q -L rootfs -d "$tree" -E root_owner=0:0 "$img" "${size}M" \
                || die "mkfs.ext4 -d failed. e2fsprogs >= 1.43 is needed for -d; on an older host run with MKFS_MODE=docker"
        fi
        log "rootfs.ext4: ${size} MiB"
        ;;
    *) die "MKFS_MODE must be auto, docker or host" ;;
    esac
}

build_rootfs() {
    local img="$OUT/rootfs.ext4" tar="$BUILD/rootfs.tar"
    build_pyz
    if up_to_date "$img" "$OUT/guest.pyz" "$HERE/Dockerfile.rootfs" "$HERE/harness-init" "$HERE/versions.env" "$HERE/build.sh"; then
        log "rootfs.ext4 up to date"; return 0
    fi
    need docker
    log "docker build $ROOTFS_IMAGE_TAG"
    docker build --quiet -f "$HERE/Dockerfile.rootfs" \
        --build-arg "ROOTFS_BASE_IMAGE=$ROOTFS_BASE_IMAGE" -t "$ROOTFS_IMAGE_TAG" "$HERE" >/dev/null
    local cid; cid="$(docker create "$ROOTFS_IMAGE_TAG")"
    trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' RETURN
    log "docker export -> $tar"
    docker export "$cid" -o "$tar"
    docker rm -f "$cid" >/dev/null
    rootfs_mkfs "$tar" "$img.tmp"
    mv "$img.tmp" "$img"
    log "built $img ($(stat -c %s "$img") bytes)"
}

# ------------------------------------------------------------- kernel
build_kernel() { fetch_verified "$KERNEL_URL" "$KERNEL_SHA256" "$OUT/vmlinux"; }

# ---------------------------------------------------------- firecracker
build_firecracker() {
    local tgz="$BUILD/firecracker.tgz"
    fetch_verified "$FIRECRACKER_URL" "$FIRECRACKER_SHA256" "$tgz"
    if [ -x "$OUT/firecracker" ] && "$OUT/firecracker" --version 2>/dev/null | grep -q "$FIRECRACKER_VERSION"; then
        log "firecracker $FIRECRACKER_VERSION present"; return 0
    fi
    tar -xzf "$tgz" -C "$BUILD"
    cp "$BUILD/release-$FIRECRACKER_VERSION-x86_64/firecracker-$FIRECRACKER_VERSION-x86_64" "$OUT/firecracker"
    cp "$BUILD/release-$FIRECRACKER_VERSION-x86_64/jailer-$FIRECRACKER_VERSION-x86_64" "$OUT/jailer"
    chmod 0755 "$OUT/firecracker" "$OUT/jailer"
    log "extracted $OUT/firecracker and $OUT/jailer"
}

# ----------------------------------------------------------- workspace
build_workspace() {
    local img="$OUT/workspace-template.img"
    [ -f "$img" ] && { log "workspace-template.img present"; return 0; }
    rm -f "$img.tmp"; truncate -s "${WORKSPACE_TEMPLATE_MIB}M" "$img.tmp"
    if command -v mkfs.ext4 >/dev/null 2>&1; then
        mkfs.ext4 -F -q -L workspace "$img.tmp"
    elif command -v docker >/dev/null 2>&1; then
        docker run --rm -v "$OUT:/out" "$ROOTFS_BASE_IMAGE" bash -euc \
            'apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends e2fsprogs >/dev/null && mkfs.ext4 -F -q -L workspace /out/workspace-template.img.tmp'
    else
        die "need mkfs.ext4 or docker for the workspace template"
    fi
    mv "$img.tmp" "$img"
    log "built $img (${WORKSPACE_TEMPLATE_MIB} MiB sparse)"
}

# ------------------------------------------------------------- version
# The identity a snapshot must match (MANIFEST.json image_version). Same formula
# as app/host/sandbox/firecracker.py image_version_from_shas().
build_version() {
    [ -f "$OUT/vmlinux" ] && [ -f "$OUT/rootfs.ext4" ] || die "version: build kernel and rootfs first"
    local k r v
    k="$(sha256sum "$OUT/vmlinux" | cut -d' ' -f1)"
    r="$(sha256sum "$OUT/rootfs.ext4" | cut -d' ' -f1)"
    v="$(printf '%s\n%s\n' "$k" "$r" | sha256sum | cut -c1-16)"
    echo "$v" > "$OUT/IMAGE_VERSION"
    log "IMAGE_VERSION $v"
}

# ------------------------------------------------------------ snapshot
# Boots the rootfs in Firecracker, waits for the guest's idle-wait, writes
# $HARNESS_VM_CACHE/base/{vmstate,mem,MANIFEST.json}. Needs /dev/kvm. Defaults
# point at out/; set any HARNESS_* to build against the deployed copies.
build_snapshot() {
    need "$PYTHON"
    [ -f "$OUT/IMAGE_VERSION" ] || build_version
    local fc="${HARNESS_FIRECRACKER_BIN:-}"
    if [ -z "$fc" ]; then
        if [ -x "$OUT/firecracker" ]; then fc="$OUT/firecracker"; else fc=firecracker; fi
    fi
    # shellcheck disable=SC2086
    HARNESS_FIRECRACKER_BIN="$fc" \
    HARNESS_KERNEL="${HARNESS_KERNEL:-$OUT/vmlinux}" \
    HARNESS_ROOTFS="${HARNESS_ROOTFS:-$OUT/rootfs.ext4}" \
    HARNESS_WORKSPACE_TEMPLATE="${HARNESS_WORKSPACE_TEMPLATE:-$OUT/workspace-template.img}" \
    HARNESS_VM_CACHE="${HARNESS_VM_CACHE:-$OUT/vm}" \
        "$PYTHON" "$HERE/build_snapshot.py" build-base ${SNAPSHOT_ARGS:-}
}

# --------------------------------------------------------------- check
check() {
    local rc=0
    for f in guest.pyz rootfs.ext4 vmlinux; do
        if [ -f "$OUT/$f" ]; then
            printf '[check] %-22s %12s bytes\n' "$f" "$(stat -c %s "$OUT/$f")"
        else
            printf '[check] %-22s MISSING\n' "$f"; rc=1
        fi
    done
    [ -f "$OUT/IMAGE_VERSION" ] && echo "[check] IMAGE_VERSION $(cat "$OUT/IMAGE_VERSION")"
    [ -f "$OUT/workspace-template.img" ] && printf '[check] %-22s %12s bytes (sparse, %s KiB used)\n' \
        workspace-template.img "$(stat -c %s "$OUT/workspace-template.img")" "$(du -k "$OUT/workspace-template.img" | cut -f1)"
    if [ -f "$OUT/vmlinux" ]; then
        sha_ok "$OUT/vmlinux" "$KERNEL_SHA256" && echo "[check] vmlinux sha256 ok ($KERNEL_VERSION)" || { echo "[check] vmlinux sha256 MISMATCH"; rc=1; }
        head -c 4 "$OUT/vmlinux" | grep -q ELF && echo "[check] vmlinux is an ELF (uncompressed, as Firecracker wants)"
    fi
    if [ -f "$OUT/guest.pyz" ]; then
        if "$PYTHON" "$OUT/guest.pyz" --help >/dev/null 2>"$BUILD/pyz-help.err"; then
            echo "[check] python3 out/guest.pyz --help: ok"
        elif grep -q "no main() yet" "$BUILD/pyz-help.err"; then
            echo "[check] python3 out/guest.pyz --help: guest.main is still a stub (imports ok)"
        else
            echo "[check] python3 out/guest.pyz --help FAILED:"; cat "$BUILD/pyz-help.err"; rc=1
        fi
        "$PYTHON" -c "import sys; sys.path.insert(0, '$OUT/guest.pyz'); import guest, protocol, guest.main" \
            && echo "[check] guest.pyz: guest + protocol import" || rc=1
    fi
    if [ -f "$OUT/rootfs.ext4" ]; then
        if command -v debugfs >/dev/null 2>&1; then
            for p in /sbin/harness-init /opt/harness/guest.pyz /usr/local/bin/python3.12 /bin/bash /usr/bin/git; do
                if debugfs -R "stat $p" "$OUT/rootfs.ext4" 2>/dev/null | grep -q 'Inode:'; then
                    echo "[check] rootfs has $p"
                else echo "[check] rootfs MISSING $p"; rc=1; fi
            done
        else
            echo "[check] (debugfs not installed; rootfs contents not inspected)"
        fi
    fi
    return $rc
}

clean() { rm -rf "$OUT" "$BUILD"; log "cleaned"; }

cmd="${1:-all}"
case "$cmd" in
    pyz) build_pyz ;;
    rootfs) build_rootfs ;;
    kernel) build_kernel ;;
    firecracker) build_firecracker ;;
    workspace) build_workspace ;;
    version) build_version ;;
    snapshot) build_snapshot ;;
    all) build_pyz; build_rootfs; build_kernel; build_workspace; build_version ;;
    check) check ;;
    clean) clean ;;
    *) die "unknown command: $cmd (pyz|rootfs|kernel|firecracker|workspace|version|snapshot|all|check|clean)" ;;
esac
