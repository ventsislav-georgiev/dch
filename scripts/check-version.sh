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
if ! grep -q '^#define DCH_VERSION PACKAGE_VERSION$' dtach.h; then
  echo "dtach.h: DCH_VERSION must be defined as PACKAGE_VERSION" >&2
  exit 1
fi
echo "ok: tag $tag matches AC_INIT $ac"
