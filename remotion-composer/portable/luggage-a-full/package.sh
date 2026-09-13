#!/usr/bin/env bash
# package.sh — produce a fully self-contained tarball of this bundle.
#
# The bundle source lives at remotion-composer/portable/luggage-a-full/.
# Assets (images, music, voice) live separately at
# remotion-composer/public/_staged/luggage-a-full/ — gitignored because
# they're regenerable by the asset pipeline. We copy them in here at
# packaging time so the tgz is truly self-contained and recipients don't
# need the parent project layout.
#
# Result: a tgz that the recipient can `tar xzf` anywhere and run
# `npm install && npx remotion render src/index.tsx LuggageAFull out.mp4`.
# The tgz's top-level directory is `luggage-a-full/`, so extracting
# creates a self-named subdir the user can `cd` into.

set -euo pipefail

cd "$(dirname "$0")"

BUNDLE_NAME="$(basename "$(pwd)")"  # "luggage-a-full"
# Bundle lives at <repo>/remotion-composer/portable/luggage-a-full/.
# Going up two levels lands in <repo>/remotion-composer/, where the
# asset pipeline's canonical output (public/_staged/luggage-a-full/) lives.
REPO_ROOT="$(cd ../.. && pwd)"
ASSETS_SRC="${REPO_ROOT}/public/_staged/luggage-a-full"

OUT="${BUNDLE_NAME}-portable.tgz"

# Drop any stale in-bundle copies / symlinks before re-copying, so the
# tarball always contains real files (not symlinks that point at paths
# on this host and break on the recipient).
echo "==> Cleaning stale in-bundle assets (if any)"
rm -rf public/_staged

echo "==> Copying assets from ${ASSETS_SRC} into bundle"
mkdir -p public/_staged/${BUNDLE_NAME}
cp -rL "${ASSETS_SRC}/images" public/_staged/${BUNDLE_NAME}/images
cp -rL "${ASSETS_SRC}/music"  public/_staged/${BUNDLE_NAME}/music
cp -rL "${ASSETS_SRC}/voice"  public/_staged/${BUNDLE_NAME}/voice

echo "==> Creating self-contained tarball: $OUT"

# Use --transform to force the top-level entry to be the bundle dir
# name, so `tar xzf` produces a `luggage-a-full/` subdir the user can cd
# into. --exclude drops the tgz itself to avoid recursive inclusion.
tar czf "${OUT}" \
    --transform "s,^\\.,${BUNDLE_NAME}," \
    --exclude="${OUT}" \
    --exclude=node_modules \
    --exclude=out \
    --exclude=dist \
    --exclude=.DS_Store \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    .

SIZE=$(du -h "${OUT}" | cut -f1)
echo "==> Done: ${OUT} (${SIZE})"
echo ""
echo "Recipient workflow:"
echo "  tar xzf ${OUT}"
echo "  cd ${BUNDLE_NAME}"
echo "  npm install"
echo "  npx remotion render src/index.tsx LuggageAFull out.mp4 \\"
echo "      --chrome-executable=/usr/bin/chromium-browser \\"
echo "      --chrome-flag=--no-sandbox \\"
echo "      --chrome-flag=--disable-gpu \\"
echo "      --chrome-flag=--disable-dev-shm-usage \\"
echo "      --codec h264"

# Clean up the copied assets so the source tree stays lean (the originals
# live at ${ASSETS_SRC} and will be re-copied on next package run).
echo "==> Cleaning in-bundle asset copies"
rm -rf public/_staged
