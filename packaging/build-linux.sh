#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Build a Linux x86_64 distribution of protoboard-designer.
#
# Output:
#   dist/protoboard-designer/                          (one-folder bundle)
#   dist/protoboard-designer-<VERSION>-linux-x86_64.tar.gz
#
# Requirements:
#   - uv (https://docs.astral.sh/uv/)
#   - pyinstaller (declared in pyproject.toml [project.optional-dependencies] dev)

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# Read version from pyproject.toml (line: version = "x.y.z").
VERSION=$(grep -E '^version\s*=' pyproject.toml | head -n1 | sed -E 's/version\s*=\s*"([^"]+)"/\1/')
if [[ -z "$VERSION" ]]; then
    echo "ERROR: could not parse version from pyproject.toml" >&2
    exit 1
fi

ARCH="$(uname -m)"
BUNDLE_NAME="protoboard-designer"
TARBALL="dist/${BUNDLE_NAME}-${VERSION}-linux-${ARCH}.tar.gz"

echo "==> Building protoboard-designer ${VERSION} for linux-${ARCH}"

echo "==> Syncing dev dependencies (includes pyinstaller)"
uv sync --extra dev

echo "==> Running PyInstaller"
rm -rf build/ dist/"${BUNDLE_NAME}"
uv run pyinstaller packaging/protoboard.spec --clean --noconfirm

if [[ ! -d "dist/${BUNDLE_NAME}" ]]; then
    echo "ERROR: PyInstaller did not produce dist/${BUNDLE_NAME}/" >&2
    exit 1
fi

echo "==> Smoke test: launching --help"
# Sanity check that the bundled binary at least imports without crashing.
# We do not need a display server for --help.
QT_QPA_PLATFORM=offscreen "dist/${BUNDLE_NAME}/protoboard-designer" --help >/dev/null || {
    echo "WARN: smoke test failed (binary did not print help cleanly)"
}

echo "==> Packing tarball"
rm -f "$TARBALL"
tar czf "$TARBALL" -C dist "${BUNDLE_NAME}"

echo
echo "Done."
echo "  Bundle:  dist/${BUNDLE_NAME}/"
echo "  Tarball: ${TARBALL}"
echo "  Size:    $(du -h "$TARBALL" | cut -f1)"
