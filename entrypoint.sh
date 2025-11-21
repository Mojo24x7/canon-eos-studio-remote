#!/bin/bash
set -e

IMG_DIR="/app/canon_image"
TARGET_DIR="${CANON_BASE_DIR:-/canon}"

# Ensure base dirs exist
mkdir -p "$TARGET_DIR" \
         "$TARGET_DIR/photos" \
         "$TARGET_DIR/tmp" \
         "$TARGET_DIR/www"

echo "[ENTRYPOINT] Using CANON_BASE_DIR=$TARGET_DIR"

# 1) server.py – copy only if missing
if [ ! -f "$TARGET_DIR/server.py" ]; then
  echo "[ENTRYPOINT] server.py missing, seeding from image"
  cp "$IMG_DIR/server.py" "$TARGET_DIR/server.py"
else
  echo "[ENTRYPOINT] server.py exists in $TARGET_DIR, keeping host version"
fi

# 2) HTML / www – copy only missing files from /app/canon_image/www
if [ -d "$IMG_DIR/www" ]; then
  for f in index.html gallery.html field.html live.html; do
    if [ -f "$IMG_DIR/www/$f" ] && [ ! -f "$TARGET_DIR/www/$f" ]; then
      echo "[ENTRYPOINT] Seeding www/$f from image"
      cp "$IMG_DIR/www/$f" "$TARGET_DIR/www/$f"
    fi
  done
else
  echo "[ENTRYPOINT] WARNING: $IMG_DIR/www not found; no HTML to seed"
fi

echo "[ENTRYPOINT] CANON_WWW_DIR=${CANON_WWW_DIR:-$TARGET_DIR/www}"
echo "[ENTRYPOINT] CANON_TMP_DIR=${CANON_TMP_DIR:-$TARGET_DIR/tmp}"

cd "$TARGET_DIR"
exec python server.py


################### THIS IS A HELPER FILE FOR FILES IN IMAGE / LOCAL FOLDERS  #########################
