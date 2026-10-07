# harness/image -- guest VM image (harness/docs/IMAGE.md). Everything runs unprivileged.
#   make image       guest.pyz + rootfs.ext4 + vmlinux + workspace-template.img  (into out/)
#   make check       sizes, sha256, `python3 out/guest.pyz --help`, rootfs contents
#   make firecracker pinned Firecracker + jailer binaries into out/ (host side)
#   make clean
.PHONY: image pyz rootfs kernel workspace firecracker check clean
image:       ; ./build.sh all
pyz:         ; ./build.sh pyz
rootfs:      ; ./build.sh rootfs
kernel:      ; ./build.sh kernel
workspace:   ; ./build.sh workspace
firecracker: ; ./build.sh firecracker
check:       ; ./build.sh check
clean:       ; ./build.sh clean
