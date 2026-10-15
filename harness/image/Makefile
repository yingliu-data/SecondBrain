# harness/image -- guest VM image (harness/docs/IMAGE.md). Everything runs unprivileged.
#   make image       guest.pyz + rootfs.ext4 + vmlinux + workspace-template.img  (into out/)
#   make check       sizes, sha256, `python3 out/guest.pyz --help`, rootfs contents
#   make firecracker pinned Firecracker + jailer binaries into out/ (host side)
#   make snapshot    base snapshot (guest idle before connecting out) into
#                    $HARNESS_VM_CACHE/base (default out/vm/base); needs /dev/kvm.
#                    SNAPSHOT_ARGS="--fixed-delay 8" skips vsock-metrics idle detection.
#   make clean
.PHONY: image pyz rootfs kernel workspace version snapshot firecracker check clean
image:       ; ./build.sh all
pyz:         ; ./build.sh pyz
rootfs:      ; ./build.sh rootfs
kernel:      ; ./build.sh kernel
workspace:   ; ./build.sh workspace
version:     ; ./build.sh version
snapshot:    ; ./build.sh snapshot
firecracker: ; ./build.sh firecracker
check:       ; ./build.sh check
clean:       ; ./build.sh clean
