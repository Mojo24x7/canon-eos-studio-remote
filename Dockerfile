##################### THIS FILE ASSUME ROOT FOLDER TO BE /canon ###################
### provide option ko keep files remote and in image ##############
####################################################################

FROM python:3.11-slim

WORKDIR /app

# System deps for gphoto2 + Pillow + exiftool + dcraw
RUN apt-get update && apt-get install -y --no-install-recommends \
    gphoto2 \
    exiftool \
    dcraw \
    libjpeg62-turbo-dev \
    libfreetype6-dev \
    && rm -rf /var/lib/apt/lists/*

# ---- Python deps from requirements-canon.txt ----
COPY requirements-canon.txt /app/requirements-canon.txt
RUN pip install --no-cache-dir -r /app/requirements-canon.txt

# ---- Canon app baked into the image (baseline/seed) ----
COPY server.py /app/canon_image/server.py
COPY www /app/canon_image/www

# Entrypoint that seeds /canon from /app/canon_image if needed
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Defaults; can be overridden with env vars
ENV CANON_BASE_DIR=/canon \
    CANON_WWW_DIR=/canon/www \
    CANON_TMP_DIR=/canon/tmp

ENTRYPOINT ["/app/entrypoint.sh"]
