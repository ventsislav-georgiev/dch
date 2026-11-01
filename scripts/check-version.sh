#!/bin/sh
# Fail unless the release tag (arg 1, default: the tag on HEAD) matches AC_INIT in configure.ac.
# usage: scripts/check-version.sh [vX.Y.Z] [path/to/configure.ac]
cd "$(dirname "$0")/.." || exit 1
tag=${1:-$(git describe --tags --exact-match 2>/dev/null)}
ac=$(sed -n 's/^AC_INIT(dch, *\([^,]*\),.*/\1/p' "${2:-configure.ac}")
if [ "v$ac" != "$tag" ]; then
  echo "version mismatch: tag '$tag' vs configure.ac AC_INIT '$ac'" >&2
  exit 1
fi
# configure is committed: a release that bumps only configure.ac builds the old version string.
if ! grep -q "^PACKAGE_VERSION='$ac'\$" "$(dirname "${2:-configure.ac}")/configure"; then
  echo "configure is stale: regenerate it (autoconf) and commit it with configure.ac" >&2
  exit 1
fi
if [ -z "$2" ] && ! git diff --quiet HEAD -- configure configure.ac; then
  echo "configure/configure.ac have uncommitted changes: the tag would not carry them" >&2
  exit 1
fi
if ! grep -q '^#define DCH_VERSION PACKAGE_VERSION$' dtach.h; then
  echo "dtach.h: DCH_VERSION must be defined as PACKAGE_VERSION" >&2
  exit 1
fi
echo "ok: tag $tag matches AC_INIT $ac"
