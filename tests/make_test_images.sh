#!/bin/bash
# Build synthetic My Cloud Home disk images for the tests. Run as root inside WSL/Linux:
#   sudo bash tests/make_test_images.sh <output-dir>
# Only loop devices created by this script are touched.
set -euo pipefail
OUT=${1:?output dir}
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d)
mkdir -p "$OUT"
LOOP=""; MD=""
cleanup() {
  mountpoint -q "$WORK/mnt" && umount "$WORK/mnt" || true
  [ -n "$MD" ] && mdadm --stop "$MD" >/dev/null 2>&1 || true
  [ -n "$LOOP" ] && losetup -d "$LOOP" || true
  rm -rf "$WORK"
}
trap cleanup EXIT
mkdir "$WORK/mnt"

# 1) Home Duo style: GPT, two md RAID1 (v1.2) members with one disk missing, ext4 4K blocks
IMG="$OUT/homeduo_raid1_ext4.img"
rm -f "$IMG"; truncate -s 200M "$IMG"
sgdisk -o -n 1:2048:+16M -t 1:FD00 -n 2:0:0 -t 2:FD00 "$IMG" >/dev/null 2>&1
P2_START=$(sgdisk -i 2 "$IMG" | awk '/First sector/{print $3}')
P2_END=$(sgdisk -i 2 "$IMG" | awk '/Last sector/{print $3}')
P2_SIZE=$((P2_END - P2_START + 1))
LOOP=$(losetup -f --show -o $((P2_START * 512)) --sizelimit $((P2_SIZE * 512)) "$IMG")
MD=/dev/md/hdtest_$$
mdadm --create "$MD" --run --quiet --metadata=1.2 --level=1 --raid-devices=2 --homehost=any --name=1 "$LOOP" missing
mkfs.ext4 -q -F -b 4096 -L hdtest -E lazy_itable_init=0,lazy_journal_init=0 "$MD"
mount "$MD" "$WORK/mnt"
python3 "$HERE/fixture_builder.py" "$WORK/mnt" --posix
mv "$WORK/expected.json" "$OUT/homeduo_raid1_ext4.expected.json"
umount "$WORK/mnt"
mdadm --stop "$MD" >/dev/null; MD=""
losetup -d "$LOOP"; LOOP=""

# 2) Bare ext3 (block maps, 1K blocks), no partition table
IMG2="$OUT/plain_ext3_1k.img"
rm -f "$IMG2"; truncate -s 96M "$IMG2"
mkfs.ext3 -q -F -b 1024 "$IMG2"
mount -o loop "$IMG2" "$WORK/mnt"
python3 -c "import sys; sys.path.insert(0, '$HERE'); import fixture_builder as f; f.build('$WORK/mnt', posix=True, big_mb=12)"
mv "$WORK/expected.json" "$OUT/plain_ext3_1k.expected.json"
umount "$WORK/mnt"
echo "built: $IMG $IMG2"
