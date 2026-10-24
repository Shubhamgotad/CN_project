#!/usr/bin/env bash
set -euo pipefail

VERSION=3.17.1
SOURCE_SHA256=84404ca8431b595e86c473d8f23d8bb102810001f15feaf610effd3b318788aa
SOURCE_URL="https://downloads.es.net/pub/iperf/iperf-${VERSION}.tar.gz"
PATCH_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/patches/iperf-3.17.1-receiver-boundaries.patch"
BASE_DIR="${1:-${TMPDIR:-/tmp}}"
mkdir -p "$BASE_DIR"
WORK_DIR="$(mktemp -d "${BASE_DIR%/}/adaptive-qos-iperf3-rb.XXXXXX")"
ARCHIVE="$WORK_DIR/iperf-${VERSION}.tar.gz"
SOURCE_DIR="$WORK_DIR/iperf-${VERSION}"

command -v curl >/dev/null
command -v sha256sum >/dev/null
command -v tar >/dev/null
command -v patch >/dev/null
command -v make >/dev/null
command -v cc >/dev/null

curl --fail --location --silent --show-error "$SOURCE_URL" --output "$ARCHIVE"
printf '%s  %s\n' "$SOURCE_SHA256" "$ARCHIVE" | sha256sum --check --status
mkdir "$SOURCE_DIR"
tar -xzf "$ARCHIVE" -C "$SOURCE_DIR" --strip-components=1

patch_sha256="$(sha256sum "$PATCH_FILE" | cut -d ' ' -f 1)"
patch --directory "$SOURCE_DIR" --strip=1 --dry-run < "$PATCH_FILE"
patch --directory "$SOURCE_DIR" --strip=1 < "$PATCH_FILE"

cat > "$SOURCE_DIR/src/aqos_receiver_boundary_build.h" <<EOF
#ifndef AQOS_RECEIVER_BOUNDARY_BUILD_H
#define AQOS_RECEIVER_BOUNDARY_BUILD_H
#define AQOS_RB_SOURCE_SHA256 "$SOURCE_SHA256"
#define AQOS_RB_PATCH_SHA256 "$patch_sha256"
#endif
EOF

cd "$SOURCE_DIR"
./configure --disable-shared
if ! grep -Eq '^#define HAVE_CLOCK_GETTIME 1$' src/iperf_config.h; then
    echo "Build refused: this target does not compile iperf_time_now with CLOCK_MONOTONIC" >&2
    exit 1
fi
if ! grep -Eq '^#define HAVE_SSL 1$' src/iperf_config.h; then
    echo "Build refused: OpenSSL EVP is required to identify the executable hash" >&2
    exit 1
fi
make -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"

test -x src/iperf3
printf 'Built prototype (not installed): %s\n' "$SOURCE_DIR/src/iperf3"
sha256sum "$SOURCE_DIR/src/iperf3"
"$SOURCE_DIR/src/iperf3" --version | head -n 1
printf 'Source archive SHA-256: %s\nPatch SHA-256: %s\n' \
    "$SOURCE_SHA256" "$patch_sha256"
