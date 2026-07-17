#!/bin/sh
# Rebuild the vendored libghostty-vt.a from the pinned upstream commit.
# Requires: git, zig 0.15.x (e.g. `brew install zig@0.15`).
#
# Usage: scripts/build-libghostty.sh [--refresh-sha] [--all]
#   --refresh-sha  rewrite the sha256 lines in vendor/libghostty-vt/COMMIT
#                  (use when bumping the pin or adding a platform)
#   --all          cross-compile every vendored platform (zig cross-compiles
#                  natively), not just the host
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
vendor="$repo/vendor/libghostty-vt"
pin=$(sed -n 's/^ghostty commit: //p' "$vendor/COMMIT")
# Fixed path, NOT $TMPDIR: ReleaseSafe blobs embed absolute source paths
# (panic/debug info), so byte-reproducibility across machines requires
# everyone to build from the same directory.
work="/tmp/dch-libghostty-build"

# platform dir -> zig -Dtarget (empty = host build)
host_platform="$(uname -s | tr 'A-Z' 'a-z')-$(uname -m)"
zig_target_for() {
	case "$1" in
	darwin-arm64)  echo aarch64-macos ;;
	darwin-x86_64) echo x86_64-macos ;;
	linux-x86_64)  echo x86_64-linux-gnu ;;
	linux-aarch64) echo aarch64-linux-gnu ;;
	*) echo "error: unknown platform $1" >&2; exit 1 ;;
	esac
}

refresh=no
platforms=$host_platform
for arg in "$@"; do
	case "$arg" in
	--refresh-sha) refresh=yes ;;
	--all) platforms="darwin-arm64 darwin-x86_64 linux-x86_64 linux-aarch64" ;;
	*) echo "error: unknown flag $arg" >&2; exit 1 ;;
	esac
done

zig=${ZIG:-zig}
command -v "$zig" >/dev/null || zig=/opt/homebrew/opt/zig@0.15/bin/zig
"$zig" version | grep -q '^0\.15\.' || {
  echo "error: need zig 0.15.x, got $("$zig" version) (set ZIG=/path/to/zig)" >&2
  exit 1
}

rm -rf "$work"
git clone --quiet https://github.com/ghostty-org/ghostty "$work"
git -C "$work" checkout --quiet "$pin"

cp -R "$work/include" "$vendor/include.new"
rm -rf "$vendor/include"
mv "$vendor/include.new" "$vendor/include"

status=0
for platform in $platforms; do
  target=$(zig_target_for "$platform")
  echo "building libghostty-vt @ $pin for $platform ($target)"
  # Fixed cache dirs for the same reason as $work: ~/.cache/zig paths end
  # up embedded in the archive and differ per machine.
  (cd "$work" && ZIG_GLOBAL_CACHE_DIR=/tmp/dch-libghostty-zig-cache \
    ZIG_LOCAL_CACHE_DIR="$work/.zig-cache" \
    "$zig" build -Demit-lib-vt=true -Doptimize=ReleaseSafe \
    -Dtarget="$target")

  mkdir -p "$vendor/lib/$platform"
  cp "$work/zig-out/lib/libghostty-vt.a" "$vendor/lib/$platform/"

  sha=$(shasum -a 256 "$vendor/lib/$platform/libghostty-vt.a" | cut -d' ' -f1)
  expected=$(sed -n "s|^\([0-9a-f]*\)  lib/$platform/libghostty-vt.a$|\1|p" "$vendor/COMMIT")

  if [ "$refresh" = yes ] || [ -z "$expected" ]; then
    grep -v "  lib/$platform/libghostty-vt.a$" "$vendor/COMMIT" > "$vendor/COMMIT.tmp"
    # keep artifact lines grouped under the header
    awk -v line="$sha  lib/$platform/libghostty-vt.a" '
      { print } /^artifacts \(sha256\):$/ { print line }' "$vendor/COMMIT.tmp" > "$vendor/COMMIT"
    rm "$vendor/COMMIT.tmp"
    echo "recorded sha256 $sha for $platform"
  elif [ "$sha" != "$expected" ]; then
    echo "error: sha256 mismatch for $platform" >&2
    echo "  built:    $sha" >&2
    echo "  recorded: $expected" >&2
    echo "(rebuild not reproducible, or pin/COMMIT out of sync; --refresh-sha to accept)" >&2
    status=1
  else
    echo "sha256 verified: $sha"
  fi
done
exit $status
