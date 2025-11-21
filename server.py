#!/usr/bin/env python3
# server.py — Canon Remote (no LiveView) with GPS tagging, EXIF, histogram,
# quick settings, presets, intervalometer, bulk gallery actions.
#
# RAW-aware:
# - /latest.jpg and /preview/<name> serve a JPEG preview (from JPEG or RAW+dcraw)
# - /photo/<name> shows preview but reads EXIF from original RAW/JPEG
# - /api/gallery returns both original URL and thumbnail URL

from flask import (
    Flask, jsonify, send_file, make_response, request,
    abort, send_from_directory, Response

)
import os, glob, time, subprocess, threading, re, json, shutil, urllib.request
from io import BytesIO
import zipfile, tempfile
import os
import logging
import requests  # make sure `requests` is installed in that venv

# ---------- Optional (EXIF + histogram) ----------
try:
    from PIL import Image, ImageDraw, ExifTags, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True  # tolerate partially written/progressive JPEGs
    PIL_OK = True
    EXIF_TAGS = {v: k for k, v in ExifTags.TAGS.items()}
    GPSTAGS = ExifTags.GPSTAGS
except Exception:
    PIL_OK = False
    EXIF_TAGS = {}
    GPSTAGS = {}

EXIFTOOL = shutil.which("exiftool")  # used for writing/reading EXIF/GPS if available
DCRAW    = shutil.which("dcraw")     # used for RAW → JPEG thumbnail (optional)

# Supported image extensions (for "latest" & gallery)
IMG_EXTS = (
    ".jpg", ".jpeg", ".JPG", ".JPEG",
    ".cr2", ".CR2", ".nef", ".NEF", ".dng", ".DNG"
)

# ---------- App & paths ----------
app = Flask(__name__, static_url_path="")

BASE_DIR = os.environ.get("CANON_BASE_DIR", "/home/pi/canon")

# Default paths
DEFAULT_photos_dir = os.path.join(BASE_DIR, "photos")
WWW_DIR            = os.environ.get("CANON_WWW_DIR", os.path.join(BASE_DIR, "www"))
TMP_DIR            = os.environ.get("CANON_TMP_DIR", os.path.join(BASE_DIR, "tmp"))
GPS_CONF           = os.environ.get("CANON_GPS_CONF", os.path.join(BASE_DIR, "gps.json"))
IMPORT_CONF        = os.environ.get("CANON_IMPORT_CONF", os.path.join(BASE_DIR, "import.json"))
PREVIEW_CONF       = os.path.join(BASE_DIR, "preview.json")

AURAFACE_NOTIFY_URL = os.environ.get("AURAFACE_NOTIFY_URL")  # e.g. "http://192.168.1.10:8091/api/new-image"
AURAFACE_UI_URL = os.environ.get("AURAFACE_UI_URL", "http://192.168.1.10:8091/")

# Folder choices for UI-level switch
PHOTO_CHOICES = {
    "canon": {
        "label": "Canon Remote (local)",
        "path": DEFAULT_photos_dir,
    },
    "auraface": {
        "label": "AuraFace shared (/photopipeline/fotos)",
        "path": "/photopipeline/fotos",
    },
}

PATHS_CONF = os.path.join(BASE_DIR, "paths.json")

ACTIVE_PHOTO_KEY = None
ACTIVE_PHOTO_PATH = None


LAST_CAPTURE_TS = 0.0
MIN_CAPTURE_GAP = 1.2  # seconds


def notify_auraface(new_path: str) -> dict:
    """
    Tell the AuraFace pipeline that a new image was saved.

    Returns a small dict for the frontend log box, like:
    {"enabled": True, "status": "ok"} or {"enabled": True, "status":"error", "error": "..."}
    """
    if not AURAFACE_NOTIFY_URL:
        logging.info("AuraFace notify skipped: AURAFACE_NOTIFY_URL not set")
        return {"enabled": False, "status": "disabled"}

    name = os.path.basename(new_path)

    # Send both path + name (robust)
    payload = {
        "path": new_path,
        "name": name,
    }

    try:
        r = requests.post(AURAFACE_NOTIFY_URL, json=payload, timeout=3.0)

        # Log non-200 with body so we can see what the pipeline complains about
        if r.status_code != 200:
            body = (r.text or "").strip()
            logging.warning(
                "AuraFace notify HTTP %s. Body: %r",
                r.status_code,
                body[:500],  # cap just in case
            )
            r.raise_for_status()

        try:
            remote = r.json()
        except Exception:
            remote = {}

        logging.info("AuraFace notify OK: %s -> %s", new_path, AURAFACE_NOTIFY_URL)
        return {
            "enabled": True,
            "status": "ok",
            "http_status": r.status_code,
            "remote_status": remote.get("status"),
        }

    except Exception as e:
        logging.warning("AuraFace notify FAILED for %s: %s", new_path, e)
        return {
            "enabled": True,
            "status": "error",
            "error": str(e),
        }


def _load_paths_conf():
    """Load last active photo folder key from JSON, or default to 'canon'."""
    if os.path.exists(PATHS_CONF):
        try:
            with open(PATHS_CONF, "r") as f:
                cfg = json.load(f)
                key = cfg.get("active_key")
                if key in PHOTO_CHOICES:
                    return key
        except Exception:
            pass
    return "canon"


def _save_paths_conf(key):
    try:
        with open(PATHS_CONF, "w") as f:
            json.dump({"active_key": key}, f)
    except Exception:
        pass


def _set_active_photos(key):
    global ACTIVE_PHOTO_KEY, ACTIVE_PHOTO_PATH
    if key not in PHOTO_CHOICES:
        key = "canon"
    ACTIVE_PHOTO_KEY = key
    ACTIVE_PHOTO_PATH = PHOTO_CHOICES[key]["path"]
    os.makedirs(ACTIVE_PHOTO_PATH, exist_ok=True)


def _photos_dir():
    """Current active photos directory (for all capture/gallery ops)."""
    if not ACTIVE_PHOTO_PATH:
        _set_active_photos(_load_paths_conf())
    return ACTIVE_PHOTO_PATH


# Initialise directories on startup
os.makedirs(_photos_dir(), exist_ok=True)
os.makedirs(WWW_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)
# Load persisted Live View hold config
try:
    lv_cfg = _load_liveview_conf()
    liveview_state["hold_wait"] = int(lv_cfg.get("hold_wait", 300))
except Exception:
    pass

cam_lock = threading.Lock()

# Cache last known status values so UI doesn’t flicker to "—" when camera is busy
_last_status_cache = {
    "battery": None,
    "shooting_mode": None,
}

# ---------- Intervalometer state ----------
interval_state = {
    "running": False,
    "interval": 0.0,
    "remaining": 0,
    "total": 0,
    "thread": None,
    "last_error": None,
}
interval_lock = threading.Lock()


import_state = {
    "running": False,
    "mode": None,        # "new" or "all"
    "target": None,      # "session" or "root"
    "total": 0,
    "done": 0,
    "imported": 0,
    "skipped": 0,
    "errors": 0,
    "current": None,
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    # AuraFace stats for imports
    "auraface_sent": 0,
    "auraface_failed": 0,
}

import_lock = threading.Lock()
import_cancel = threading.Event()   # <-- NEW



# ---------- Live View state ----------
LIVEVIEW_KEYS = [
    "/main/actions/viewfinder",
    "/main/actions/eosviewfinder",
]

LIVEVIEW_CONF = os.path.join(BASE_DIR, "liveview.json")

liveview_state = {
    "running": False,
    "hold_wait": 300,   # default seconds for --wait-event
    "last_error": None,
}

_liveview_proc = None
_liveview_lock = threading.Lock()

# NEW: track MJPEG live-stream process
_live_mjpeg_proc = None
_live_mjpeg_lock = threading.Lock()

# ---------- Shooting presets ----------
PRESETS = {
    "Portrait": {
        "/main/imgsettings/iso": "400",
        "/main/capturesettings/drivemode": "Single",
        "/main/capturesettings/focusmode": "AI Focus",
        "/main/capturesettings/afmethod": "Auto",
    },
    "Low Light": {
        "/main/imgsettings/iso": "1600",
        "/main/capturesettings/drivemode": "Single",
        "/main/capturesettings/focusmode": "One Shot",
    },
    "Action": {
        "/main/imgsettings/iso": "800",
        "/main/capturesettings/drivemode": "Continuous",
        "/main/capturesettings/focusmode": "AI Servo",
    },
    "Studio": {
        "/main/imgsettings/iso": "100",
        "/main/capturesettings/drivemode": "Single",
        "/main/capturesettings/focusmode": "One Shot",
    },
}

# ---------- Quick-settings keys (multiple fallbacks per setting) ----------
QUICK_KEYS = {
    "iso": [
        "/main/imgsettings/iso",
        "/main/capturesettings/iso",
        "/main/imgsettings/sensitivity",
    ],
    "ss": [
        "/main/capturesettings/shutterspeed",
    ],
    "ap": [
        "/main/capturesettings/aperture",
        "/main/capturesettings/f-number",
    ],
    "dm": [
        "/main/capturesettings/drivemode",
    ],
    "fm": [
        "/main/capturesettings/focusmode",
    ],
    "afm": [
        "/main/capturesettings/afmethod",
        "/main/capturesettings/afmode",
    ],
}
# remembers which actual key worked last for each id (iso/ss/ap/...)
QUICK_ACTIVE_KEY = {}

# ---------- helpers ----------

def _set_liveview(on: bool) -> bool:
    """
    Try to enable/disable Live View using common Canon gphoto2 keys.
    Returns True if any key worked.
    """
    val = "1" if on else "0"
    for k in LIVEVIEW_KEYS:
        try:
            subprocess.check_output(
                ["gphoto2", "--set-config", f"{k}={val}"],
                text=True,
                stderr=subprocess.STDOUT
            )
            return True
        except subprocess.CalledProcessError:
            continue
    return False


def _load_liveview_conf():
    if os.path.exists(LIVEVIEW_CONF):
        try:
            with open(LIVEVIEW_CONF, "r") as f:
                cfg = json.load(f)
                cfg.setdefault("hold_wait", 300)
                return cfg
        except Exception:
            pass
    return {"hold_wait": 300}


def _save_liveview_conf(cfg):
    try:
        os.makedirs(os.path.dirname(LIVEVIEW_CONF), exist_ok=True)
        with open(LIVEVIEW_CONF, "w") as f:
            json.dump(cfg, f)
        return True
    except Exception:
        return False


def _liveview_proc_running():
    """Return True if the background gphoto2 live-view-hold process is alive."""
    global _liveview_proc
    if _liveview_proc is None:
        return False
    if _liveview_proc.poll() is None:
        return True
    # process finished
    _liveview_proc = None
    liveview_state["running"] = False
    return False


def _liveview_proc_start(wait_s: int) -> bool:
    """
    Start a background gphoto2 process that:
      gphoto2 --set-config /main/actions/viewfinder=1 --wait-event=<wait_s>s

    This keeps the mirror up and output=PC for roughly wait_s seconds.
    """
    global _liveview_proc
    with _liveview_lock:
        # Already running? Don't start a second one.
        if _liveview_proc is not None and _liveview_proc.poll() is None:
            return True

        try:
            wait_s = int(wait_s or 0)
        except Exception:
            wait_s = 0
        if wait_s < 5:
            wait_s = 5
        if wait_s > 3600:
            wait_s = 3600

        cmd = [
            "gphoto2",
            "--set-config", "/main/actions/viewfinder=1",
            f"--wait-event={wait_s}s",
        ]
        try:
            _liveview_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            liveview_state["running"] = True
            liveview_state["hold_wait"] = wait_s
            liveview_state["last_error"] = None

            # persist default
            cfg = _load_liveview_conf()
            cfg["hold_wait"] = wait_s
            _save_liveview_conf(cfg)

            return True
        except Exception as e:
            liveview_state["running"] = False
            liveview_state["last_error"] = str(e)
            _liveview_proc = None
            return False


def _liveview_proc_stop():
    """Stop the background live-view-hold process and drop Live View on the camera."""
    global _liveview_proc
    with _liveview_lock:
        if _liveview_proc is None:
            liveview_state["running"] = False
            return
        try:
            _liveview_proc.terminate()
            try:
                _liveview_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _liveview_proc.kill()
        except Exception:
            pass
        finally:
            _liveview_proc = None
            liveview_state["running"] = False
            try:
                _set_liveview(False)
            except Exception:
                pass



def _no_cache_send(path, mimetype="image/jpeg"):
    resp = make_response(send_file(path, mimetype=mimetype))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

def _latest_image_path(root=None, exts=IMG_EXTS):
    """
    Return the absolute path of the newest image under the photos root.

    - Considers JPEG and RAW extensions via IMG_EXTS.
    - Uses file modification time (mtime), so anything we touch in the preview
      worker becomes the 'last shot' for the UI.
    """
    if root is None:
        root = _photos_dir()

    newest_path = None
    newest_mtime = 0.0

    for base, dirs, files in os.walk(root):
        for name in files:
            # Only consider known image types (JPEG + RAW)
            if os.path.splitext(name)[1] not in exts:
                continue

            path = os.path.join(base, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue

            if mtime > newest_mtime:
                newest_mtime = mtime
                newest_path = path

    return newest_path




def _parse_get_config(out):
    val, choices = None, []
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Current:"):
            val = s.split("Current:",1)[1].strip()
        elif s.startswith("Choice:"):
            parts = s.split(" ", 2)
            if len(parts) >= 3:
                choices.append(parts[2].strip())
    return {"value": val, "choices": choices}

def _gp_get_config_safe(key):
    try:
        with cam_lock:
            out = subprocess.check_output(
                ["gphoto2","--get-config", key],
                text=True, stderr=subprocess.STDOUT
            )
        return _parse_get_config(out)
    except subprocess.CalledProcessError:
        return None

def _gp_set_value_or_index(key, val):
    try:
        subprocess.check_output(
            ["gphoto2","--set-config", f"{key}={val}"],
            text=True, stderr=subprocess.STDOUT
        )
        return True
    except subprocess.CalledProcessError:
        info = _gp_get_config_safe(key)
        if not info or not info.get("choices"):
            return False
        try:
            idx = info["choices"].index(val)
        except ValueError:
            return False
        subprocess.check_output(
            ["gphoto2","--set-config-index", key, str(idx)],
            text=True, stderr=subprocess.STDOUT
        )
        return True

def _gp_first_value(keys, cache_key=None):
    """
    Try a list of gphoto2 config keys, return first non-empty 'Current:' value.
    If nothing is readable (camera busy / key missing), fall back to last cached
    value if cache_key is provided.
    """
    for k in keys:
        info = _gp_get_config_safe(k)
        v = info.get("value") if info else None
        if v not in (None, "", "N/A"):
            if cache_key:
                _last_status_cache[cache_key] = v
            return v
    if cache_key:
        return _last_status_cache.get(cache_key)
    return None

def _storage_info():
    try:
        with cam_lock:
            out = subprocess.check_output(
                ["gphoto2", "--storage-info"],
                text=True,
                stderr=subprocess.DEVNULL
            )
    except Exception:
        return {"left": None, "capacity": None, "free_bytes": None, "capacity_bytes": None}
    left = cap = free_b = cap_b = None
    for line in out.splitlines():
        s = line.strip().lower()
        if s.startswith("free space (images):"):
            try: left = int(re.sub(r"[^\d]", "", s.split(":",1)[1]))
            except: pass
        elif s.startswith("capacity (images):"):
            try: cap = int(re.sub(r"[^\d]", "", s.split(":",1)[1]))
            except: pass
        elif s.startswith("free space (bytes):"):
            try: free_b = int(re.sub(r"[^\d]", "", s.split(":",1)[1]))
            except: pass
        elif s.startswith("capacity (bytes):"):
            try: cap_b = int(re.sub(r"[^\d]", "", s.split(":",1)[1]))
            except: pass
    return {
        "left": left, "capacity": cap,
        "free_bytes": free_b, "capacity_bytes": cap_b
    }


def _load_import_conf():
    """
    Keep track of the highest camera file index we imported.
    Stored in IMPORT_CONF as: {"last_index": <int>}
    """
    if os.path.exists(IMPORT_CONF):
        try:
            with open(IMPORT_CONF, "r") as f:
                cfg = json.load(f)
                cfg.setdefault("last_index", 0)
                return cfg
        except Exception:
            pass
    return {"last_index": 0}


def _save_import_conf(cfg):
    try:
        os.makedirs(os.path.dirname(IMPORT_CONF), exist_ok=True)
        with open(IMPORT_CONF, "w") as f:
            json.dump(cfg, f)
        return True
    except Exception:
        return False


def _list_camera_files():
    """
    Use gphoto2 --list-files and return a list of:
    [ {"index": 1, "name": "IMG_0001.JPG", "ts": 1760951988}, ... ]

    'ts' is the numeric timestamp from the last column (when present).
    """
    try:
        with cam_lock:
            out = subprocess.check_output(
                ["gphoto2", "--list-files"],
                text=True,
                stderr=subprocess.STDOUT,
            )
    except subprocess.CalledProcessError:
        return []

    files = []
    for line in out.splitlines():
        line = line.strip()
        # only real file lines start with '#'
        if not line.startswith("#"):
            continue

        parts = line.split()
        # we expect at least: #IDX FILENAME rd SIZE KB MIMETYPE [TS]
        if len(parts) < 3:
            continue

        # index is parts[0] like "#128"
        try:
            idx = int(parts[0].lstrip("#"))
        except ValueError:
            continue

        # filename is always the second token: IMG_4315.JPG
        fname = parts[1]

        # last token may be numeric timestamp
        ts = None
        last = parts[-1]
        if last.isdigit():
            try:
                ts = int(last)
            except ValueError:
                ts = None

        files.append({"index": idx, "name": fname, "ts": ts})

    return files




def _pull_latest_from_camera_for_preview():
    """
    Lightweight 'tethered' behaviour using gphoto2 --list-files.

    - Reads all files, including their numeric timestamp.
    - Considers only stills (JPEG/RAW) for preview.
    - Tracks the highest camera timestamp we've mirrored in preview.json (last_ts).
    - Copies only a camera file that is NEWER than last_ts / last_index.
    - If nothing on the camera is newer, it does nothing (so local remote
      captures keep being the 'latest' image).
    - Respects preview.json["enabled"]: when false, does nothing at all.
    """
    cfg = _load_preview_conf()
    if not cfg.get("enabled", True):
        print("[PREVIEW] sync disabled in preview.json; skipping camera poll")
        return

    root = _photos_dir()
    pattern = os.path.join(root, "%f.%C")  # %f = camera filename, %C = extension

    print("[PREVIEW] sync: starting, root =", root)

    last_idx = int(cfg.get("last_index", 0) or 0)
    last_ts  = int(cfg.get("last_ts", 0) or 0)

    print("[PREVIEW] last_index =", last_idx, "last_ts =", last_ts)

    # Ask camera for file list
    files = _list_camera_files()
    print("[PREVIEW] camera files count =", len(files))
    if not files:
        return

    # Only consider still images for preview (ignore MOVs etc.)
    stills = [
        f for f in files
        if f["name"].lower().endswith((".jpg", ".jpeg", ".cr2", ".nef", ".dng"))
    ]
    print("[PREVIEW] still images count =", len(stills))
    if not stills:
        return

    # Decide which file is "latest NEW" on camera
    ts_available = any(f.get("ts") for f in stills)
    if ts_available:
        # only consider files strictly newer than last_ts
        newer = [f for f in stills if f.get("ts") and f["ts"] > last_ts]
        if not newer:
            print("[PREVIEW] no camera file newer than last_ts; nothing to mirror")
            return
        target = max(newer, key=lambda f: f["ts"])
        print("[PREVIEW] newer than last_ts →",
              target["index"], target["name"], target.get("ts"))
    else:
        # fall back to index-based if no numeric timestamps
        newer = [f for f in stills if f["index"] > last_idx]
        if not newer:
            print("[PREVIEW] no camera file newer than last_index; nothing to mirror")
            return
        target = max(newer, key=lambda f: f["index"])
        print("[PREVIEW] newer than last_index →",
              target["index"], target["name"])

    target_idx = int(target["index"])
    print("[PREVIEW] target index =", target_idx, "name =", target["name"])

    # Try a few times in case of transient "device busy"
    attempts = 3
    delay = 0.8

    for i in range(attempts):
        try:
            with cam_lock:
                out = subprocess.check_output(
                    [
                        "gphoto2",
                        "--get-file", str(target_idx),
                        "--force-overwrite",    # always refresh local copy of THIS file
                        "--filename", pattern,
                    ],
                    text=True,
                    stderr=subprocess.STDOUT,
                )

            out_str = (out or "").strip()
            print("[PREVIEW] gphoto2 output:", out_str)

            # Parse local path from gphoto2 output
            local_path = None
            for line in out_str.splitlines():
                line = line.strip()
                if "Saving file as" in line:
                    local_path = line.split("Saving file as", 1)[1].strip()
                elif "Skip existing file" in line:
                    local_path = line.split("Skip existing file", 1)[1].strip()

            if local_path:
                try:
                    if os.path.exists(local_path):
                        os.utime(local_path, None)
                        print("[PREVIEW] touched local file to bump mtime:", local_path)
                    else:
                        print("[PREVIEW] local_path from gphoto2 does not exist:", local_path)
                except Exception as e:
                    print("[PREVIEW] utime failed:", repr(e))
            else:
                print("[PREVIEW] could not parse local_path from gphoto2 output")

            # SUCCESS: we processed this *new* file
            cfg["last_index"] = target_idx
            if target.get("ts"):
                cfg["last_ts"] = int(target["ts"])
            _save_preview_conf(cfg)
            print("[PREVIEW] updated preview.json →",
                  "last_index =", cfg["last_index"],
                  "last_ts =", cfg.get("last_ts"))
            return

        except subprocess.CalledProcessError as e:
            msg = (e.output or "").lower()
            print("[PREVIEW] sync failed (CalledProcessError, attempt",
                  i + 1, "/", attempts, "):", (e.output or "").strip())

            transient = any(
                kw in msg
                for kw in [
                    "could not claim the usb device",
                    "device or resource busy",
                    "device busy",
                    "ptp i/o error",
                ]
            )

            if transient and i < attempts - 1:
                time.sleep(delay)
                continue  # retry
            break  # non-transient or last attempt → give up

        except Exception as e:
            print("[PREVIEW] sync exception (attempt",
                  i + 1, "/", attempts, "):", repr(e))
            break






def _detect_cameras():
    cams = []
    try:
        out = subprocess.check_output(
            ["gphoto2","--auto-detect"],
            text=True, stderr=subprocess.DEVNULL
        )
        found = False
        for line in out.splitlines():
            if not found and line.strip().startswith("Model"):
                found = True
                continue
            if found:
                parts = [p.strip() for p in re.split(r"\s{2,}", line.strip()) if p.strip()]
                if len(parts) >= 2:
                    cams.append({"model": parts[0], "port": parts[1]})
    except Exception:
        pass
    return cams

#-------- Import worker ------------------
# -------- Import worker ------------------
def _import_worker(mode="new", session=True):
    """
    Background worker that imports files from the camera card.

    mode    = "new" or "all"
    session = True  → use <photos_dir>/session_YYYYMMDD
              False → use <photos_dir> directly
    """
    root_dir = _photos_dir()
    cancelled = False  # track if we stopped via /api/import/stop

    try:
        cfg = _load_import_conf()
        last_idx = int(cfg.get("last_index", 0) or 0)

        # IMPORTANT: this is the camera card, via gphoto2
        files = _list_camera_files()
        if not files:
            with import_lock:
                import_state["running"] = False
                import_state["total"] = 0
                import_state["last_error"] = "No files listed by gphoto2"
                import_state["finished_at"] = time.time()
            return

        if mode == "new":
            selected = [f for f in files if f["index"] > last_idx]
        else:
            selected = files

        with import_lock:
            import_state.update({
                "total": len(selected),
                "done": 0,
                "imported": 0,
                "skipped": 0,
                "errors": 0,
                "current": None,
                "last_error": None,
                "auraface_sent": 0,
                "auraface_failed": 0,
            })


        if not selected:
            with import_lock:
                import_state["running"] = False
                import_state["finished_at"] = time.time()
            return

        # decide target directory on disk
        if session:
            ts = time.strftime("%Y%m%d")
            target_dir = os.path.join(root_dir, f"session_{ts}")
        else:
            target_dir = root_dir
        os.makedirs(target_dir, exist_ok=True)

        max_idx = last_idx

        for item in selected:
            # ---- CANCEL CHECKS ----
            if import_cancel.is_set():
                cancelled = True
                break

            with import_lock:
                # keep old flag behaviour too
                if not import_state.get("running"):
                    cancelled = True
                    break
                import_state["current"] = item["name"]

            idx = item["index"]
            fname = item["name"]
            dest = os.path.join(target_dir, fname)

            try:
                out = subprocess.check_output(
                    [
                        "gphoto2",
                        "--get-file", str(idx),
                        "--skip-existing",
                        "--filename", dest,
                    ],
                    text=True,
                    stderr=subprocess.STDOUT,
                )

                existed_msg = ("File exists" in out) or ("already exists" in out)

                if os.path.exists(dest) and not existed_msg:
                    # really downloaded a new file
                    with import_lock:
                        import_state["imported"] += 1
                else:
                    # file already present → count as skipped
                    with import_lock:
                        import_state["skipped"] += 1

                if idx > max_idx:
                    max_idx = idx

            except subprocess.CalledProcessError as e:
                with import_lock:
                    import_state["errors"] += 1
                    import_state["last_error"] = e.output
            finally:
                with import_lock:
                    import_state["done"] += 1

        # Only advance last_index if we weren’t cancelled mid-way
        if max_idx > last_idx and not cancelled:
            cfg["last_index"] = max_idx
            _save_import_conf(cfg)

    except Exception as e:
        # hard crash
        with import_lock:
            import_state["last_error"] = f"worker crashed: {e}"
    finally:
        with import_lock:
            import_state["running"] = False
            import_state["current"] = None
            import_state["finished_at"] = time.time()
            if cancelled and not import_state.get("last_error"):
                import_state["last_error"] = "cancelled by user"






# ---------- RAW → JPEG preview ----------
def _ensure_jpeg_preview(path):
    """
    Given an original image path (JPEG or RAW), return a JPEG path suitable for preview.
    - If it's already a JPEG → return it.
    - If there's a sibling .jpg/.jpeg with same base → use that.
    - If dcraw is available and it's RAW → extract embedded thumbnail (dcraw -e) and use it.
    - If nothing works → return None.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        return path

    base = os.path.splitext(path)[0]
    for e in (".jpg", ".jpeg", ".JPG", ".JPEG"):
        cand = base + e
        if os.path.isfile(cand):
            return cand

    if not DCRAW:
        return None

    try:
        subprocess.check_output(
            [DCRAW, "-e", path],
            stderr=subprocess.STDOUT,
            text=True
        )
        thumb = path + ".thumb.jpg"
        if os.path.isfile(thumb):
            return thumb
    except Exception:
        pass

    return None

# ---------- GPS config ----------
def _load_gps():
    if os.path.exists(GPS_CONF):
        try:
            with open(GPS_CONF, "r") as f:
                cfg = json.load(f)
                cfg.setdefault("enabled", False)
                cfg.setdefault("mode", "manual")
                cfg.setdefault("lat", None)
                cfg.setdefault("lon", None)
                cfg.setdefault("ssid_map", {})
                return cfg
        except Exception:
            pass
    return {"enabled": False, "mode": "manual", "lat": None, "lon": None, "ssid_map": {}}

def _save_gps(cfg):
    try:
        os.makedirs(os.path.dirname(GPS_CONF), exist_ok=True)
        with open(GPS_CONF, "w") as f:
            json.dump(cfg, f)
        return True
    except Exception:
        return False

def _current_ssid():
    # nmcli first
    try:
        out = subprocess.check_output(
            ["nmcli","-t","-f","ACTIVE,SSID","dev","wifi"],
            text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            parts = line.strip().split(":")
            if len(parts) >= 2 and parts[0] == "yes":
                return parts[1]
    except Exception:
        pass
    # fallback: iwgetid
    try:
        out = subprocess.check_output(
            ["iwgetid","-r"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
        if out:
            return out
    except Exception:
        pass
    return None

def _geolocate_ip():
    try:
        with urllib.request.urlopen(
            "http://ip-api.com/json/?fields=status,lat,lon",
            timeout=3.5
        ) as r:
            j = json.loads(r.read().decode("utf-8"))
            if j.get("status") == "success":
                return float(j.get("lat")), float(j.get("lon"))
    except Exception:
        pass
    try:
        with urllib.request.urlopen("https://ipinfo.io/loc", timeout=3.5) as r:
            txt = r.read().decode("utf-8").strip()
            if "," in txt:
                la, lo = txt.split(",", 1)
                return float(la), float(lo)
    except Exception:
        pass
    return None, None

def _tag_gps(path, lat, lon):
    if not EXIFTOOL:
        return False
    try:
        lat_ref = "N" if float(lat) >= 0 else "S"
        lon_ref = "E" if float(lon) >= 0 else "W"
        subprocess.check_output([
            EXIFTOOL, "-overwrite_original",
            f"-GPSLatitude={abs(float(lat))}",
            f"-GPSLatitudeRef={lat_ref}",
            f"-GPSLongitude={abs(float(lon))}",
            f"-GPSLongitudeRef={lon_ref}",
            path
        ], stderr=subprocess.STDOUT, text=True)
        return True
    except Exception:
        return False

def _resolve_coords_for_auto(cfg):
    ssid = _current_ssid()
    if ssid and isinstance(cfg.get("ssid_map"), dict):
        entry = cfg["ssid_map"].get(ssid)
        if entry and "lat" in entry and "lon" in entry:
            try:
                return float(entry["lat"]), float(entry["lon"]), ssid, "ssid"
            except Exception:
                pass
    lat, lon = _geolocate_ip()
    if lat is not None and lon is not None:
        return lat, lon, ssid, "ip"
    return None, None, ssid, None

def _auto_tag_if_enabled(jpeg_path):
    cfg = _load_gps()
    details = {
        "auto": False, "ok": False, "mode": cfg.get("mode"),
        "source": None, "ssid": _current_ssid()
    }
    if not (cfg.get("enabled") and EXIFTOOL and os.path.isfile(jpeg_path)):
        return details
    mode = cfg.get("mode","manual")
    lat = lon = None
    source = None
    ssid = _current_ssid()
    if mode == "manual":
        try:
            lat = float(cfg.get("lat"))
            lon = float(cfg.get("lon"))
            source = "manual"
        except Exception:
            details.update({
                "auto": True, "ok": False,
                "mode":"manual", "source": None, "ssid": ssid
            })
            return details
    else:
        lat, lon, ssid, source = _resolve_coords_for_auto(cfg)
        if lat is None or lon is None:
            details.update({
                "auto": True, "ok": False,
                "mode":"auto", "source": None, "ssid": ssid
            })
            return details
    ok = _tag_gps(jpeg_path, lat, lon)
    details.update({
        "auto": True, "ok": ok, "mode": mode,
        "source": source, "ssid": ssid,
        "lat": lat, "lon": lon
    })
    return details

# ---------- EXIF read + histogram ----------
def _rational_to_float(v):
    try:
        if isinstance(v, tuple) and v[1]:
            return float(v[0]) / float(v[1])
        return float(v)
    except Exception:
        return None

def _dms_to_deg(dms, ref):
    try:
        d = _rational_to_float(dms[0]) or 0.0
        m = _rational_to_float(dms[1]) or 0.0
        s = _rational_to_float(dms[2]) or 0.0
        deg = d + (m/60.0) + (s/3600.0)
        if ref in ("S","W"):
            deg = -deg
        return round(deg, 6)
    except Exception:
        return None

def _read_gps_with_exiftool(path):
    try:
        out = subprocess.check_output(
            [EXIFTOOL, "-GPSLatitude", "-GPSLongitude", "-n", "-s3", path],
            text=True, stderr=subprocess.DEVNULL
        )
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        if len(lines) >= 2:
            return float(lines[0]), float(lines[1])
    except Exception:
        pass
    return None

def _read_basic_with_exiftool(path):
    """
    Read core EXIF (incl. GPS) using exiftool JSON output.
    This avoids depending on line order and fixes the
    'GPS showing up as exposure/ISO' bug.
    """
    try:
        out = subprocess.check_output(
            [
                EXIFTOOL,
                "-j", "-n",              # JSON, numeric values
                "-Make",
                "-Model",
                "-LensModel",
                "-DateTimeOriginal",
                "-ISO",                  # <--- simpler ISO tag (works on your T3i)
                "-EXIF:ISOSpeedRatings",
                "-EXIF:PhotographicSensitivity",
                "-FNumber",
                "-ExposureTime",
                "-FocalLength",
                "-GPSLatitude",
                "-GPSLongitude",
                path,
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )

        arr = json.loads(out)
        if not arr:
            return None
        d = arr[0]

        # ISO: try ISO, then fall back to older tags
        iso = (
            d.get("ISO")
            or d.get("EXIF:ISOSpeedRatings")
            or d.get("EXIF:PhotographicSensitivity")
        )

        # f-number
        fnum = None
        fn = d.get("FNumber")
        if isinstance(fn, (int, float)):
            fnum = round(float(fn), 1)

        # exposure time
        exposure = None
        et = d.get("ExposureTime")
        if isinstance(et, (int, float)):
            exposure = f"{et:.4f}s"
        elif isinstance(et, str):
            exposure = et if et.endswith("s") else et + "s"

        # focal length
        focl = None
        fl = d.get("FocalLength")
        if isinstance(fl, (int, float)):
            focl = int(round(fl))

        return {
            "make": d.get("Make") or None,
            "model": d.get("Model") or None,
            "lens": d.get("LensModel") or None,
            "datetime": d.get("DateTimeOriginal") or None,
            "iso": iso,
            "fnumber": fnum,
            "exposure": exposure,
            "focal_length": focl,
            "gps_lat": d.get("GPSLatitude"),
            "gps_lon": d.get("GPSLongitude"),
        }
    except Exception:
        return None



def _read_exif(path):
    """Read EXIF from ORIGINAL file (RAW or JPEG), prefer exiftool for RAW."""
    info = {"file": os.path.basename(path), "size": os.path.getsize(path)}
    # If exiftool exists, let it handle all RAW/JPEG cases first (more robust)
    if EXIFTOOL:
        exif = _read_basic_with_exiftool(path)
        if exif:
            info.update(exif)
            return info
    # Fallback to PIL for JPEGs
    if PIL_OK:
        try:
            with Image.open(path) as im:
                ex = getattr(im, "_getexif", lambda: None)() or {}
                exif = {ExifTags.TAGS.get(k, str(k)): v for k, v in ex.items()}
                info.update({
                    "make": exif.get("Make"),
                    "model": exif.get("Model"),
                    "lens": exif.get("LensModel") or exif.get("UndefinedTag:0xA434"),
                    "datetime": exif.get("DateTimeOriginal") or exif.get("DateTime"),
                    "iso": exif.get("ISOSpeedRatings") or exif.get("PhotographicSensitivity"),
                    "fnumber": None, "exposure": None, "focal_length": None,
                    "gps_lat": None, "gps_lon": None
                })
                fn = exif.get("FNumber")
                if isinstance(fn, tuple) and fn[1]:
                    info["fnumber"] = round(fn[0]/fn[1], 1)
                elif isinstance(fn, (int, float)):
                    info["fnumber"] = fn
                et = exif.get("ExposureTime")
                if isinstance(et, tuple) and et[1]:
                    info["exposure"] = f"{et[0]}/{et[1]}s"
                elif isinstance(et, (int, float)):
                    info["exposure"] = f"{et:.4f}s"
                fl = exif.get("FocalLength")
                if isinstance(fl, tuple) and fl[1]:
                    info["focal_length"] = round(fl[0]/fl[1])
                elif isinstance(fl, (int, float)):
                    info["focal_length"] = int(fl)
                gps = ex.get(EXIF_TAGS.get("GPSInfo"))
                if gps and GPSTAGS:
                    gps_parsed = {GPSTAGS.get(k, k): v for k, v in gps.items()}
                    lat = gps_parsed.get("GPSLatitude")
                    lat_ref = gps_parsed.get("GPSLatitudeRef")
                    lon = gps_parsed.get("GPSLongitude")
                    lon_ref = gps_parsed.get("GPSLongitudeRef")
                    if lat and lat_ref and lon and lon_ref:
                        info["gps_lat"] = _dms_to_deg(lat, lat_ref)
                        info["gps_lon"] = _dms_to_deg(lon, lon_ref)
                if (info.get("gps_lat") is None or info.get("gps_lon") is None) and EXIFTOOL:
                    et2 = _read_gps_with_exiftool(path)
                    if et2:
                        info["gps_lat"], info["gps_lon"] = et2
                return info
        except Exception:
            pass
    return info

def _histogram_png(path):
    if not PIL_OK:
        return None
    try:
        with Image.open(path) as im:
            if im.mode not in ("RGB", "L"):
                try:
                    im = im.convert("RGB")
                except Exception:
                    pass
            im = im.convert("L")
            hist = im.histogram()[:256]
        w, h, pad = 256, 120, 6
        maxc = max(hist) or 1
        img = Image.new("RGB", (w, h), (12, 18, 32))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, w-1, h-1], outline=(36, 48, 72))
        for x, c in enumerate(hist):
            bar_h = int((c / maxc) * (h - 2*pad))
            if bar_h > 0:
                d.line([(x, h-1-pad), (x, h-1-pad-bar_h)], fill=(210,210,210))
        bio = BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        return bio
    except Exception:
        return None


def _jpeg_with_hist_overlay(jpeg_path, exif=None):
    """
    Create an in-memory JPEG with:
    - histogram overlaid in the bottom-right
    - exposure strip in the bottom-left (f/number, shutter, ISO, GPS)
    Returns a BytesIO or None on failure.
    """
    if not PIL_OK:
        print("[OVERLAY] PIL not available, skipping overlay")
        return None

    try:
        from PIL import Image, ImageDraw, ImageFont

        print("[OVERLAY] called with:", jpeg_path)

        base = Image.open(jpeg_path).convert("RGBA")
        print("[OVERLAY] base image:", base.width, "x", base.height, "mode:", base.mode)

        # ---------- Histogram (unchanged style) ----------
        hist_bio = _histogram_png(jpeg_path)
        if hist_bio:
            with Image.open(hist_bio) as hist_img:
                hist_img = hist_img.convert("RGBA")

                target_w = max(160, base.width // 4)
                ratio = target_w / hist_img.width
                target_h = int(hist_img.height * ratio)
                hist_img = hist_img.resize((target_w, target_h), Image.LANCZOS)

                pad = 8
                overlay_w = target_w + pad * 2
                overlay_h = target_h + pad * 2

                overlay = Image.new("RGBA", (overlay_w, overlay_h), (2, 8, 23, 220))
                overlay.paste(hist_img, (pad, pad))

                bx = base.width - overlay_w - 24
                by = base.height - overlay_h - 24
                base.alpha_composite(overlay, (bx, by))
                print("[OVERLAY] histogram composited at:", bx, by)

        # ---------- Exposure / metadata strip ----------
        text_parts = []

        if isinstance(exif, dict):
            # Aperture
            fnum = exif.get("fnumber")
            if isinstance(fnum, (int, float)):
                # f/4.0 -> f/4
                f_clean = f"{fnum:.1f}".rstrip("0").rstrip(".")
                text_parts.append(f"f/{f_clean}")

            # Shutter: 0.0250s -> 1/40s
            exp = exif.get("exposure")
            if exp:
                exp_str = str(exp).rstrip("s")
                try:
                    val = float(exp_str)
                    if val >= 1:
                        shutter = f"{int(round(val))}s"
                    else:
                        denom = max(1, int(round(1.0 / val)))
                        shutter = f"1/{denom}s"
                except Exception:
                    shutter = str(exp)
                text_parts.append(shutter)

            # ISO
            iso = exif.get("iso")
            if iso is not None:
                text_parts.append(f"ISO {iso}")

            # GPS (only if present)
            glat = exif.get("gps_lat")
            glon = exif.get("gps_lon")
            if glat is not None and glon is not None:
                text_parts.append(f"{glat:.5f}, {glon:.5f}")

        raw_text = "   ".join(str(p) for p in text_parts) if text_parts else ""
        if not raw_text:
            raw_text = "TEST SHOT"

        # Ensure ASCII safe
        safe_text = raw_text.encode("ascii", "ignore").decode("ascii") or "TEST SHOT"
        print("[OVERLAY] final text to draw:", repr(safe_text))

        draw = ImageDraw.Draw(base)

        # Font sizing: ~1/40 of height, min 28px
        font_size = max(28, base.height // 40)
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", font_size)
        except Exception as e:
            print("[OVERLAY] TrueType font load failed, using default:", e)
            font = ImageFont.load_default()

        pad_x = 18
        pad_y = 10

        # Measure text
        try:
            bbox = draw.textbbox((0, 0), safe_text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
        except Exception as e:
            print("[OVERLAY] textbbox failed:", e)
            tw = len(safe_text) * font_size // 2
            th = font_size

        box_w = tw + pad_x * 2
        box_h = th + pad_y * 2

        # Bottom-left strip
        x0 = 24
        y0 = base.height - box_h - 24

        bg = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 190))
        base.alpha_composite(bg, (x0, y0))

        draw = ImageDraw.Draw(base)
        draw.text((x0 + pad_x, y0 + pad_y), safe_text, font=font, fill=(248, 250, 252))

        out = BytesIO()
        base.convert("RGB").save(out, format="JPEG", quality=92)
        out.seek(0)
        return out

    except Exception as e:
        print("testshot overlay failed:", repr(e))
        return None

def _load_preview_conf():
    """
    Track which camera file we last mirrored for live preview, and whether
    card-sync is enabled.

    Stored JSON example:
      {"last_index": 271, "last_ts": 1762948800, "enabled": true}
    """
    if os.path.exists(PREVIEW_CONF):
        try:
            with open(PREVIEW_CONF, "r") as f:
                cfg = json.load(f)
                cfg.setdefault("last_index", 0)
                cfg.setdefault("last_ts", 0)
                cfg.setdefault("enabled", True)
                return cfg
        except Exception:
            pass
    # default: sync enabled
    return {"last_index": 0, "last_ts": 0, "enabled": True}




def _save_preview_conf(cfg):
    try:
        os.makedirs(os.path.dirname(PREVIEW_CONF), exist_ok=True)
        with open(PREVIEW_CONF, "w") as f:
            json.dump(cfg, f)
        return True
    except Exception:
        return False





# ---------- Static HTML ----------
@app.get("/")
def root_index():
    return send_from_directory(WWW_DIR, "index.html")

@app.get("/gallery")
def gallery_view():
    return send_from_directory(WWW_DIR, "gallery.html")

@app.get("/live")
def live_page():
    return send_from_directory(WWW_DIR, "live.html")


# NEW: field / HDMI layout
@app.get("/field")
def field_index():
    return send_from_directory(WWW_DIR, "field.html")
# ---------- Preview route (JPEG from JPEG or RAW) ----------
@app.get("/preview/<name>")
def preview_serve(name):
    root = _photos_dir()
    raw_path = os.path.join(root, os.path.basename(name))
    if not os.path.isfile(raw_path):
        return ("Not found", 404)
    jpeg_path = _ensure_jpeg_preview(raw_path)
    if not jpeg_path:
        return ("No JPEG preview available (install dcraw or enable RAW+JPEG)", 404)
    return _no_cache_send(jpeg_path, "image/jpeg")


# ---------- Photo page (dynamic with prev/next) ----------
PHOTO_HTML_TOP = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Photo</title>
<style>
body{margin:0;padding:12px;background:#020817;color:#e5e7eb;font-family:ui-sans-serif,system-ui}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:16px;color:#9ca3af;margin:0 0 8px;word-break:break-all}
.meta{font-size:12px;color:#9ca3af;margin:6px 0 12px}
img.main{width:100%;height:auto;border-radius:10px;background:#000;display:block}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:8px}
a{color:#93c5fd;text-decoration:none}
a:hover{text-decoration:underline}
.btn{background:#111827;border:1px solid #22304a;color:#e5e7eb;border-radius:10px;
     padding:8px 11px;text-decoration:none;font-size:13px;display:inline-flex;align-items:center;gap:4px}
.btn:hover{background:#0d1526}
.btn.primary{background:#f97316;color:#111827;border-color:#f97316}
.navrow{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}
.navbtn{min-width:90px;justify-content:center}
.navbtn.disabled{opacity:.4;pointer-events:none}
.badge{font-size:11px;color:#9ca3af}
</style>
<div class=wrap>
"""
PHOTO_HTML_BOTTOM = """
<script>
(function(){
  const img = document.querySelector('img.main');
  if(!img) return;

  let startX = null;
  let startY = null;
  const THRESHOLD = 40; // px

  img.addEventListener('touchstart', function(e){
    if(!e.touches || e.touches.length !== 1) return;
    const t = e.touches[0];
    startX = t.clientX;
    startY = t.clientY;
  }, {passive:true});

  img.addEventListener('touchend', function(e){
    if(startX === null || !e.changedTouches || !e.changedTouches.length) {
      startX = null; startY = null;
      return;
    }
    const t = e.changedTouches[0];
    const dx = t.clientX - startX;
    const dy = t.clientY - startY;
    startX = null;
    startY = null;

    // require mostly horizontal swipe
    if(Math.abs(dx) < THRESHOLD || Math.abs(dx) < Math.abs(dy)) return;

    if(dx < 0){
      // swipe left → next (newer)
      const next = img.dataset.next;
      if(next){
        window.location.href = '/photo/' + encodeURIComponent(next);
      }
    }else{
      // swipe right → previous (older)
      const prev = img.dataset.prev;
      if(prev){
        window.location.href = '/photo/' + encodeURIComponent(prev);
      }
    }
  }, {passive:true});

  // Optional: support keyboard arrows on desktop
  window.addEventListener('keydown', function(e){
    if(e.key === 'ArrowLeft'){
      const prev = img.dataset.prev;
      if(prev){
        window.location.href = '/photo/' + encodeURIComponent(prev);
      }
    } else if(e.key === 'ArrowRight'){
      const next = img.dataset.next;
      if(next){
        window.location.href = '/photo/' + encodeURIComponent(next);
      }
    }
  });
})();
</script>
</div>
"""


@app.get("/photo/<name>")
def photo_view(name):
    root = _photos_dir()
    path = os.path.join(root, name)
    if not os.path.isfile(path):
        abort(404)

    files = sorted(
        glob.glob(os.path.join(root, "*.jpg")) +
        glob.glob(os.path.join(root, "*.JPG")) +
        glob.glob(os.path.join(root, "*.cr2")) +
        glob.glob(os.path.join(root, "*.CR2")),
        key=os.path.getmtime,
        reverse=True
    )
    names = [os.path.basename(p) for p in files]
    prev_name = next_name = None
    if name in names:
        idx = names.index(name)
        if idx > 0:
            next_name = names[idx - 1]    # newer
        if idx + 1 < len(names):
            prev_name = names[idx + 1]    # older

    info = _read_exif(path)

    bits = []
    for k in ("datetime","model","lens","fnumber","exposure","iso","focal_length","gps_lat","gps_lon"):
        v = info.get(k)
        if v is None:
            continue
        label_map = {
            "datetime":"Time","model":"Camera","lens":"Lens","fnumber":"ƒ",
            "exposure":"Shutter","iso":"ISO","focal_length":"Focal",
            "gps_lat":"GPS","gps_lon":"GPS"
        }
        label = label_map[k]
        if k == "gps_lat":
            continue
        if k == "gps_lon":
            lat = info.get("gps_lat")
            lon = info.get("gps_lon")
            if lat is not None and lon is not None:
                bits.append(f"<b>GPS</b>: {lat:.6f}, {lon:.6f}")
        else:
            if k == "focal_length":
                v = f"{v}mm"
            bits.append(f"<b>{label}</b>: {v}")
    meta_html = " • ".join(bits) if bits else "—"
    hist_url = f"/hist/{name}.png"

    nav_html = "<div class='navrow'>"
    if prev_name:
        nav_html += f"<a class='btn navbtn' href='/photo/{prev_name}'>⟵ Previous</a>"
    else:
        nav_html += "<span class='btn navbtn disabled'>⟵ Previous</span>"
    nav_html += "<span class='badge'>Browse captured photos</span>"
    if next_name:
        nav_html += f"<a class='btn navbtn' href='/photo/{next_name}'>Next ⟶</a>"
    else:
        nav_html += "<span class='btn navbtn disabled'>Next ⟶</span>"
    nav_html += "</div>"

    prev_attr = prev_name or ""
    next_attr = next_name or ""

    html = (
        PHOTO_HTML_TOP +
        nav_html +
        f"<h1>{name}</h1><div class=meta>{meta_html}</div>"
        f"<img class='main' src='/preview/{name}' alt='{name}' "
        f"data-prev='{prev_attr}' data-next='{next_attr}'>"
        "<div class='row'>"
        f"<a class='btn primary' href='/file/{name}' download>Download</a>"
        f"<a class='btn' href='{hist_url}' target='_blank'>Histogram</a>"
        f"<a class='btn' href='/'>Back to Remote</a>"
        f"<a class='btn' href='/gallery'>Full Gallery</a>"
        "</div>" +
        PHOTO_HTML_BOTTOM
    )

    return html


@app.get("/latest/view")
def latest_view():
    p = _latest_image_path()
    if not p:
        abort(404)
    return photo_view(os.path.basename(p))

@app.get("/latest/file")
def latest_file():
    """Original latest file (RAW or JPEG)."""
    p = _latest_image_path()
    if not p:
        abort(404)
    return _no_cache_send(p, "application/octet-stream")

# ---------- Capture core ----------
def _capture_one():
    """
    Internal capture helper used by /capture and intervalometer.
    Returns (ok: bool, filename: str|None, gps_details: dict, auraface_info: dict|None).

    Robust version:
    - Retries gphoto2 on transient "device busy" / "PTP I/O" errors.
    """

    ts = time.strftime("%Y%m%d-%H%M%S")
    root = _photos_dir()
    pattern = os.path.join(root, f"{ts}.%C")

    def _do_capture():
        return subprocess.check_output(
            [
                "gphoto2",
                "--capture-image-and-download",
                "--filename",
                pattern,
                "--force-overwrite",
            ],
            stderr=subprocess.STDOUT,
            text=True,
        )

    attempts = 2
    delay = 0.6  # seconds between retries

    try:
        for i in range(attempts):
            try:
                out = _do_capture()
                logging.info("gphoto2 capture attempt %d/%d OK", i + 1, attempts)
                break  # success → leave loop
            except subprocess.CalledProcessError as e:
                err_txt = (e.output or "").lower()
                logging.warning(
                    "gphoto2 capture attempt %d/%d failed: %s",
                    i + 1, attempts, e.output
                )

                # transient errors: USB busy / PTP I/O / generic busy
                transient = any(
                    kw in err_txt
                    for kw in [
                        "could not claim the usb device",
                        "device or resource busy",
                        "device busy",
                        "ptp i/o error",
                        "i/o error",
                        "busy",
                    ]
                )
                if transient and i < attempts - 1:
                    time.sleep(delay)
                    continue  # retry
                # non-transient OR last attempt → rethrow
                raise

        # If we got here without raising, capture succeeded
        saved = sorted(
            glob.glob(os.path.join(root, f"{ts}.*")),
            key=os.path.getmtime,
            reverse=True,
        )
        fname = os.path.basename(saved[0]) if saved else ""
        gps_details = {}
        auraface_info = None

        if fname:
            full = os.path.join(root, fname)

            # GPS auto-tag on JPEG preview
            jpeg = _ensure_jpeg_preview(full)
            if jpeg and jpeg.lower().endswith((".jpg", ".jpeg")):
                gps_details = _auto_tag_if_enabled(jpeg) or {}

            # notify AuraFace (optional)
            auraface_info = notify_auraface(full)

        return True, fname, gps_details, auraface_info

    except subprocess.CalledProcessError as e:
        logging.error("gphoto2 capture failed after retries: %s", e.output)
        with interval_lock:
            interval_state["last_error"] = e.output
        return False, None, {}, None
    except Exception as e:
        logging.exception("Unexpected error during capture")
        with interval_lock:
            interval_state["last_error"] = str(e)
        return False, None, {}, None



# ---------- Capture test shot ----------
def _capture_testshot():
    """
    Capture one image with current settings into TMP_DIR.
    Returns (ok: bool, full_path: str|None).
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    pattern = os.path.join(TMP_DIR, f"test-{ts}.%C")
    try:
        subprocess.check_output(
            [
                "gphoto2",
                "--capture-image-and-download",
                "--filename", pattern,
                "--force-overwrite",
            ],
            stderr=subprocess.STDOUT, text=True
        )
        saved = sorted(
            glob.glob(os.path.join(TMP_DIR, f"test-{ts}.*")),
            key=os.path.getmtime, reverse=True
        )
        full = saved[0] if saved else None
        return bool(full), full
    except subprocess.CalledProcessError as e:
        logging.error("gphoto2 capture failed: %s", e.output)
        with interval_lock:
            interval_state["last_error"] = e.output
        return False, None
    except Exception as e:
        logging.exception("Unexpected error during capture")
        with interval_lock:
            interval_state["last_error"] = str(e)
        return False, None




# ---------- Capture & images ----------
@app.get("/capture")
def capture():
    """
    Single capture endpoint used by the main UI.
    - Enforces a small cooldown between shots.
    - Stops liveview-hold if running.
    - Returns JSON with filename, GPS tagging result and AuraFace notify info.
    """
    global LAST_CAPTURE_TS

    with cam_lock:
        # cooldown: if previous capture is too recent, wait a bit
        now = time.time()
        delta = now - LAST_CAPTURE_TS
        if delta < MIN_CAPTURE_GAP:
            time.sleep(MIN_CAPTURE_GAP - delta)
        LAST_CAPTURE_TS = time.time()

        # If Live View hold is running, stop it first to avoid "device busy"
        if _liveview_proc_running():
            _liveview_proc_stop()

        # Make sure Live View is off before capture
        #_set_liveview(False)

        ok, fname, gps_details, auraface_info = _capture_one()

    if not ok or not fname:
        return jsonify(
            status="Error",
            error=interval_state.get("last_error") or "capture failed"
        ), 500

    return jsonify(
        status="OK",
        file=fname,
        gps=gps_details,
        auraface=auraface_info,
    )




@app.get("/latest.jpg")
def latest_jpg():
    """
    JPEG preview of the latest image (RAW or JPEG).

    - Finds newest image via _latest_image_path (JPEG or RAW).
    - Uses _ensure_jpeg_preview() so RAWs get a JPEG thumbnail.
    - Returns a tiny 1x1 GIF placeholder if nothing is available.
    """
    p = _latest_image_path()
    if p:
        jpeg = _ensure_jpeg_preview(p)
        if jpeg:
            return _no_cache_send(jpeg, "image/jpeg")

    # Fallback: a 1x1 transparent GIF to avoid broken image icon
    return (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
        b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
        b"\x00\x02\x02D\x01\x00;",
        200,
        {
            "Content-Type": "image/gif",
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )



@app.get("/testshot.jpg")
def testshot_jpg():
    """
    Capture a test image with current exposure settings, do NOT save to gallery,
    return a JPEG with a histogram + exposure overlay. Files in TMP_DIR are cleaned up.
    """
    print("[TESTSHOT] /testshot.jpg called")

    with cam_lock:
        ok, full = _capture_testshot()
    print("[TESTSHOT] capture result ok:", ok, "full path:", full)

    if not ok or not full:
        print("[TESTSHOT] capture failed, last_error:", interval_state.get("last_error"))
        return ("Test shot failed", 500)

    # read EXIF from the original test shot
    exif = _read_exif(full) if os.path.isfile(full) else None
    print("[TESTSHOT] exif from _read_exif:", json.dumps(exif, indent=2, default=str) if isinstance(exif, dict) else exif)

    jpeg = _ensure_jpeg_preview(full)
    print("[TESTSHOT] jpeg preview path:", jpeg)

    if not jpeg:
        # cleanup
        base = os.path.splitext(full)[0]
        try:
            for p in glob.glob(base + ".*"):
                print("[TESTSHOT] cleaning up (no jpeg):", p)
                os.remove(p)
        except Exception as e:
            print("[TESTSHOT] cleanup error:", repr(e))
        return ("No JPEG preview available for test shot", 500)

    bio = _jpeg_with_hist_overlay(jpeg, exif)
    print("[TESTSHOT] overlay buffer is None?", bio is None)

    if not bio:
        # fallback: just send the JPEG content, but still clean up files
        bio = BytesIO()
        with open(jpeg, "rb") as f:
            bio.write(f.read())
        bio.seek(0)

    # Clean up the temp RAW/JPEG so the test shot is not stored
    base = os.path.splitext(full)[0]
    try:
        for p in glob.glob(base + ".*"):
            print("[TESTSHOT] cleaning up tmp file:", p)
            os.remove(p)
    except Exception as e:
        print("[TESTSHOT] cleanup error:", repr(e))

    resp = send_file(
        bio,
        mimetype="image/jpeg",
        download_name="testshot.jpg"
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    print("[TESTSHOT] response ready")
    return resp





@app.get("/latest_info")
def latest_info():
    # Mirror the latest camera shot into the active folder for live preview
    _pull_latest_from_camera_for_preview()

    p = _latest_image_path()
    if not p:
        return jsonify({})
    info = _read_exif(p)          # EXIF from original RAW/JPEG
    info["file"] = os.path.basename(p)
    return jsonify(info)



@app.get("/latest_hist.png")
def latest_hist_png():
    p = _latest_image_path()
    if not p:
        return ("No image available", 404)
    jpeg = _ensure_jpeg_preview(p) or p
    bio = _histogram_png(jpeg)
    if not bio:
        return ("Histogram not available (install pillow or image unreadable)", 404)

    resp = send_file(
        bio,
        mimetype="image/png",
        download_name="histogram.png"
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/hist/<name>.png")
def hist_named(name):
    root = _photos_dir()
    path = os.path.join(root, name)
    if not os.path.isfile(path):
        return ("Not found", 404)
    jpeg = _ensure_jpeg_preview(path) or path
    bio = _histogram_png(jpeg)
    if not bio:
        return ("Histogram not available (install pillow or image unreadable)", 404)

    resp = send_file(
        bio,
        mimetype="image/png",
        download_name=f"{name}.hist.png"
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp



@app.get("/api/gallery")
def api_gallery():
    limit = request.args.get("limit", type=int)
    root = _photos_dir()
    files = sorted(
        glob.glob(os.path.join(root, "*")),
        key=os.path.getmtime, reverse=True
    )
    files = [p for p in files if os.path.splitext(p)[1].lower() in IMG_EXTS]
    if limit:
        files = files[:max(1, min(limit, 240))]
    else:
        files = files[:240]
    items = []
    for p in files:
        name = os.path.basename(p)
        items.append({
            "name": name,
            "url": f"/file/{name}",        # original (RAW or JPEG)
            "thumb": f"/preview/{name}",   # JPEG preview
            "ts": int(os.path.getmtime(p))
        })
    return jsonify(items)


@app.get("/file/<name>")
def file_serve(name):
    """Serve original file (RAW or JPEG) as octet-stream."""
    root = _photos_dir()
    path = os.path.join(root, os.path.basename(name))
    if os.path.isfile(path):
        return _no_cache_send(path, "application/octet-stream")
    return ("Not found", 404)


# ---------- Bulk gallery operations ----------
def _safe_photo_path(name):
    safe = os.path.basename(name)
    root = _photos_dir()
    path = os.path.join(root, safe)
    if os.path.commonpath([root, path]) != root:
        return None
    return path


@app.post("/api/gallery/bulk-zip")
def gallery_bulk_zip():
    data = request.get_json(silent=True) or {}
    names = data.get("files") or []
    if not isinstance(names, list) or not names:
        return jsonify(error="no files"), 400
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
            for name in names:
                path = _safe_photo_path(name)
                if path and os.path.isfile(path):
                    z.write(path, arcname=name)
        return send_file(
            tmp.name,
            mimetype="application/zip",
            as_attachment=True,
            download_name="photos.zip"
        )
    finally:
        pass

@app.post("/api/gallery/bulk-delete")
def gallery_bulk_delete():
    data = request.get_json(silent=True) or {}
    names = data.get("files") or []
    if not isinstance(names, list) or not names:
        return jsonify(error="no files"), 400
    deleted = []
    for name in names:
        path = _safe_photo_path(name)
        if path and os.path.isfile(path):
            try:
                os.remove(path)
                deleted.append(name)
            except Exception:
                pass
    return jsonify(status="OK", deleted=deleted)

@app.post("/api/gallery/bulk-move")
def gallery_bulk_move():
    data = request.get_json(silent=True) or {}
    names = data.get("files") or []
    target = (data.get("target") or "").strip()
    if not isinstance(names, list) or not names:
        return jsonify(error="no files"), 400
    if not target:
        return jsonify(error="missing target"), 400
    target = re.sub(r"[^A-Za-z0-9_\-\.]", "_", target)
    root = _photos_dir()
    dest_dir = os.path.join(root, target)
    os.makedirs(dest_dir, exist_ok=True)
    moved = []
    for name in names:
        src = _safe_photo_path(name)
        if src and os.path.isfile(src):
            dst = os.path.join(dest_dir, os.path.basename(name))
            try:
                os.rename(src, dst)
                moved.append(name)
            except Exception:
                pass
    return jsonify(status="OK", moved=moved, target=target)



# ---------- Import from camera card ----------
@app.post("/api/import/start")
def import_start():
    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "new").lower()
    if mode not in ("new", "all"):
        mode = "new"
    session_flag = bool(data.get("session", True))

    with import_lock:
        if import_state.get("running"):
            return jsonify(error="import already running"), 400

        # reset state for a fresh run
        import_state.update({
            "running": True,
            "mode": mode,
            "target": "session" if session_flag else "root",
            "total": 0,
            "done": 0,
            "imported": 0,
            "skipped": 0,
            "errors": 0,
            "current": None,
            "started_at": time.time(),
            "finished_at": None,
            "last_error": None,
            "auraface_sent": 0,
            "auraface_failed": 0,
        })

        import_cancel.clear()   # <-- important

        t = threading.Thread(
            target=_import_worker,
            args=(mode, session_flag),
            daemon=True,
        )
        t.start()

    return jsonify(status="OK")




@app.get("/api/import/status")
def import_status():
    """
    Return current import progress.
    """
    with import_lock:
        st = dict(import_state)
    latest = _latest_image_path()
    st["latest_file"] = os.path.basename(latest) if latest else None
    return jsonify(st)


@app.post("/api/import/stop")
def import_stop():
    """
    Request cancellation of running import.
    Worker will notice import_cancel and exit between files.
    """
    with import_lock:
        if not import_state.get("running"):
            return jsonify(status="no import running"), 200

        import_cancel.set()
        import_state["last_error"] = "cancel requested"

    return jsonify(status="stopping"), 200




# ---------- Quick settings ----------
@app.get("/api/config/quick")
def cfg_quick():
    result = {}
    for field_id, key_list in QUICK_KEYS.items():
        parsed = None
        chosen_key = None
        # prefer previously working key if present
        first_try_keys = []
        if field_id in QUICK_ACTIVE_KEY:
            first_try_keys.append(QUICK_ACTIVE_KEY[field_id])
        first_try_keys += [k for k in key_list if k not in first_try_keys]

        for key in first_try_keys:
            info = _gp_get_config_safe(key)
            if info and (info.get("choices") or info.get("value") not in (None, "", "N/A")):
                parsed = info
                chosen_key = key
                break

        if chosen_key:
            QUICK_ACTIVE_KEY[field_id] = chosen_key
            result[field_id] = parsed
        else:
            defaults = {
                "iso": {"value":"Auto","choices":["Auto"]},
                "ss":  {"value":"Auto","choices":["Auto"]},
                "ap":  {"value":"Auto","choices":["Auto"]},
                "dm":  {"value":"Single","choices":["Single"]},
                "fm":  {"value":"AI Focus","choices":["AI Focus","One Shot","AI Servo"]},
                "afm": {"value":"Auto","choices":["Auto"]},
            }
            result[field_id] = defaults.get(field_id, {"value": None, "choices": []})
    return jsonify(result)

# NEW: explicit quick-settings refresh endpoint
@app.get("/api/config/quick/force")
def cfg_quick_force():
    """
    Clear cached quick-setting keys and re-probe all gphoto2 keys.
    Useful when switching cameras or modes and the UI wants a hard refresh.
    """
    QUICK_ACTIVE_KEY.clear()
    return cfg_quick()

@app.post("/api/config/set")
def cfg_set():
    data = request.get_json(silent=True) or {}
    key_id = data.get("key","").strip()
    val    = data.get("value","").strip()
    if not key_id or val == "":
        return jsonify(error="missing key or value"), 400

    # key_id here is the actual gphoto key from UI, but for safety,
    # if it looks like one of our logical IDs (iso/ss/ap/...), map it.
    key = key_id
    if key_id in QUICK_KEYS:
        key = QUICK_ACTIVE_KEY.get(key_id) or QUICK_KEYS[key_id][0]

    with cam_lock:
        if not _gp_set_value_or_index(key, val):
            return jsonify(error=f"set failed for {key}={val}"), 500
    info = _gp_get_config_safe(key)
    return jsonify(status="OK", key=key, value=(info.get("value") if info else val))

# ---------- Presets ----------
@app.get("/api/presets")
def api_presets():
    return jsonify(sorted(PRESETS.keys()))

@app.post("/api/presets/apply")
def api_presets_apply():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    preset = PRESETS.get(name)
    if not preset:
        return jsonify(error="unknown preset"), 400
    applied = []
    failed = []
    with cam_lock:
        for key, val in preset.items():
            if _gp_set_value_or_index(key, val):
                applied.append({"key": key, "value": val})
            else:
                failed.append({"key": key, "value": val})
    return jsonify(status="OK", name=name, applied=applied, failed=failed)

# ---------- Camera status / list / network ----------
@app.get("/api/status")
def api_status():
    battery = _gp_first_value([
        "/main/status/batterylevel",
        "/main/status/battery",
        "/main/status/batterystatus",
        "/main/status/batterycharge",
        "/main/status/Battery Level",   # some Canons expose this name
    ], cache_key="battery")

    shooting_mode = _gp_first_value([
        "/main/capturesettings/shootingmode",
        "/main/status/capturemode",
        "/main/capturesettings/capturemode",
        "/main/capturesettings/exposuremode",
        "/main/capturesettings/autoexposuremode",
        "/main/status/shootingmode",
    ], cache_key="shooting_mode")

    lens_name = _gp_first_value([
        "/main/status/lensname",
        "/main/status/lens",
    ])

    shutter_counter = _gp_first_value([
        "/main/status/shuttercounter",
    ])

    avail_shots = _gp_first_value([
        "/main/status/availableshots",
    ])
    st = _storage_info()
    cams = _detect_cameras()
    latest = _latest_image_path()
    latest_name = os.path.basename(latest) if latest else None
    return jsonify({
        "battery": battery,
        "shooting_mode": shooting_mode,
        "images_left": st.get("left"),
        "images_capacity": st.get("capacity"),
        "free_bytes": st.get("free_bytes"),
        "capacity_bytes": st.get("capacity_bytes"),
        "cameras": cams,
        "latest_file": latest_name,
        "lens_name": lens_name,
        "shutter_counter": shutter_counter,
        "availableshots": avail_shots,
    })


@app.get("/api/cameras")
def api_cameras():
    return jsonify(_detect_cameras())

@app.get("/api/net/ssid")
def api_net_ssid():
    return jsonify({"ssid": _current_ssid()})

@app.get("/api/folders")
def api_folders():
    """List available photo folders and which one is active."""
    current = ACTIVE_PHOTO_KEY or _load_paths_conf()
    items = {}
    for key, meta in PHOTO_CHOICES.items():
        path = meta["path"]
        items[key] = {
            "label": meta.get("label", key),
            "path": path,
            "exists": os.path.isdir(path),
        }
    # optional hint for UI integration with AuraFace
    auraface_ui = os.environ.get("AURAFACE_UI_URL", "http://192.168.1.10:8091/")
    return jsonify({
        "active": current,
        "folders": items,
        "auraface_ui": auraface_ui,
    })


@app.post("/api/folders/set")
def api_folders_set():
    """Switch active photo folder."""
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if key not in PHOTO_CHOICES:
        return jsonify(error="unknown folder key"), 400
    _set_active_photos(key)
    _save_paths_conf(key)
    return jsonify(status="OK", active=key, path=_photos_dir())

@app.get("/api/preview/config")
def preview_get_config():
    """
    Return whether 'mirror from camera card' sync is enabled.
    Stored in preview.json as {"last_index": ..., "last_ts": ..., "enabled": bool}.
    """
    cfg = _load_preview_conf()
    return jsonify({"enabled": bool(cfg.get("enabled", True))})


@app.post("/api/preview/config")
def preview_set_config():
    """
    Toggle camera-card preview sync (used by the UI checkbox).
    """
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", False))

    cfg = _load_preview_conf()
    cfg["enabled"] = enabled
    ok = _save_preview_conf(cfg)
    if not ok:
        return jsonify(status="Failed", error="could not write preview config"), 500
    return jsonify(status="OK", enabled=enabled)


# ---------- GPS endpoints ----------
@app.get("/api/gps/get")
def gps_get():
    return jsonify(_load_gps())

@app.post("/api/gps/set")
def gps_set():
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", False))
    mode = data.get("mode","manual").strip().lower()
    lat = data.get("lat")
    lon = data.get("lon")
    cfg = _load_gps()
    cfg["enabled"] = enabled
    cfg["mode"] = "auto" if mode == "auto" else "manual"
    if cfg["mode"] == "manual":
        if enabled:
            try:
                cfg["lat"] = float(lat) if lat not in (None,"") else None
                cfg["lon"] = float(lon) if lon not in (None,"") else None
            except Exception:
                return jsonify(error="invalid lat/lon"), 400
        else:
            cfg["lat"], cfg["lon"] = lat, lon
    ok = _save_gps(cfg)
    return jsonify(status="OK" if ok else "Failed")

@app.post("/api/gps/tag-latest")
def gps_tag_latest():
    p = _latest_image_path()
    if not p:
        return jsonify(error="no image"), 400
    if not EXIFTOOL:
        return jsonify(error="exiftool not installed"), 400
    jpeg = _ensure_jpeg_preview(p) or p
    res = _auto_tag_if_enabled(jpeg)
    if not res.get("auto"):
        return jsonify(error="auto tagging disabled"), 400
    return jsonify(status="OK" if res.get("ok") else "Failed", **res)

@app.get("/api/gps/detect")
def gps_detect_now():
    cfg = _load_gps()
    lat, lon, ssid, source = _resolve_coords_for_auto(cfg)
    if lat is None or lon is None:
        return jsonify({})
    return jsonify({"lat": lat, "lon": lon, "ssid": ssid, "source": source})

@app.post("/api/gps/bind-ssid")
def gps_bind_ssid():
    data = request.get_json(silent=True) or {}
    ssid = (data.get("ssid") or "").strip()
    lat = data.get("lat")
    lon = data.get("lon")
    if not ssid:
        return jsonify(error="missing ssid"), 400
    try:
        lat = float(lat)
        lon = float(lon)
    except Exception:
        return jsonify(error="invalid lat/lon"), 400
    cfg = _load_gps()
    cfg.setdefault("ssid_map", {})
    cfg["ssid_map"][ssid] = {"lat": lat, "lon": lon}
    ok = _save_gps(cfg)
    return jsonify(status="OK" if ok else "Failed", ssid=ssid, lat=lat, lon=lon)


@app.get("/api/gps/where")
def gps_where():
    cfg = _load_gps()
    enabled = bool(cfg.get("enabled"))
    mode = cfg.get("mode", "manual")

    # If tagging is disabled, we only return enabled flag
    if not enabled:
        # best-effort preview: try to compute a location, but mark as disabled
        lat = lon = None
        ssid = _current_ssid()
        source = None

        if mode == "manual":
            try:
                lat = float(cfg.get("lat"))
                lon = float(cfg.get("lon"))
                source = "manual"
            except Exception:
                lat = lon = None
        else:
            lat, lon, ssid, source = _resolve_coords_for_auto(cfg)

        if lat is None or lon is None:
            return jsonify({"enabled": False})
        return jsonify({
            "enabled": False,
            "lat": lat, "lon": lon,
            "ssid": ssid,
            "source": source or "preview"
        })

    # Tagging is enabled
    if mode == "manual":
        try:
            lat = float(cfg.get("lat"))
            lon = float(cfg.get("lon"))
            return jsonify({
                "enabled": True,
                "lat": lat, "lon": lon,
                "ssid": _current_ssid(),
                "source": "manual"
            })
        except Exception:
            return jsonify({"enabled": True})

    # auto mode + enabled
    lat, lon, ssid, source = _resolve_coords_for_auto(cfg)
    if lat is None or lon is None:
        return jsonify({"enabled": True})
    return jsonify({
        "enabled": True,
        "lat": lat, "lon": lon,
        "ssid": ssid,
        "source": source
    })


# ---------- Intervalometer ----------
def _interval_worker(interval_s, count):
    with interval_lock:
        interval_state["running"] = True
        interval_state["interval"] = interval_s
        interval_state["remaining"] = count
        interval_state["total"] = count
        interval_state["last_error"] = None

    while True:
        with interval_lock:
            if not interval_state["running"]:
                break
            if interval_state["remaining"] <= 0:
                interval_state["running"] = False
                break
        with cam_lock:
            ok, _, _, _ = _capture_one()

        with interval_lock:
            if not ok:
                interval_state["running"] = False
                break
            interval_state["remaining"] -= 1
        if interval_s <= 0:
            continue
        time.sleep(interval_s)

    with interval_lock:
        interval_state["running"] = False

@app.post("/api/interval/start")
def interval_start():
    data = request.get_json(silent=True) or {}
    interval_s = float(data.get("interval", 0))
    count = int(data.get("count", 0))
    if interval_s < 0 or count <= 0:
        return jsonify(error="invalid interval or count"), 400
    with interval_lock:
        if interval_state["running"]:
            return jsonify(error="interval already running"), 400
        t = threading.Thread(
            target=_interval_worker,
            args=(interval_s, count),
            daemon=True
        )
        interval_state["thread"] = t
        t.start()
    return jsonify(status="OK")

@app.post("/api/interval/stop")
def interval_stop():
    with interval_lock:
        interval_state["running"] = False
    return jsonify(status="OK")

@app.get("/api/interval/status")
def interval_status():
    with interval_lock:
        return jsonify({
            "running": interval_state["running"],
            "interval": interval_state["interval"],
            "remaining": interval_state["remaining"],
            "total": interval_state["total"],
            "last_error": interval_state["last_error"],
        })


# ---------- Live View ----------
# ---------- Live View (hold via --wait-event) ----------
@app.post("/api/live/start")
def live_start():
    data = request.get_json(silent=True) or {}
    wait_s = data.get("wait_seconds") or data.get("wait") or liveview_state.get("hold_wait") or 300
    try:
        wait_s = int(wait_s)
    except Exception:
        wait_s = liveview_state.get("hold_wait") or 300

    ok = _liveview_proc_start(wait_s)
    if not ok:
        detail = liveview_state.get("last_error") or "Failed to start gphoto2 live view session"
        return jsonify(status="Error", detail=detail), 500
    return jsonify(
        status="OK",
        running=True,
        wait_seconds=int(liveview_state.get("hold_wait") or wait_s),
    )


@app.post("/api/live/stop")
def live_stop():
    _liveview_proc_stop()
    return jsonify(status="OK", running=False)


@app.get("/api/live/status")
def live_status():
    running = _liveview_proc_running()
    return jsonify({
        "running": running,
        "wait_seconds": int(liveview_state.get("hold_wait") or 0),
        "last_error": liveview_state.get("last_error"),
    })



@app.get("/live.jpg")
def live_frame():
    """
    Return one Live View frame as JPEG.
    Assumes Live View is already enabled via /api/live/start.
    """
    try:
        # avoid fighting with the long-running hold-session
        if _liveview_proc_running():
            return jsonify(
                error="liveview hold is running; stop it before requesting single preview frames"
            ), 409

        with cam_lock:
            data = subprocess.check_output(
                ["gphoto2", "--capture-preview", "--stdout"],
                stderr=subprocess.DEVNULL
            )

        # data is raw JPEG bytes from gphoto2
        return Response(data, mimetype="image/jpeg")

    except subprocess.CalledProcessError as e:
        logging.error("gphoto2 --capture-preview failed: %s", e.output)
        return jsonify(error="live preview failed"), 500
    except Exception as e:
        logging.exception("Unexpected error in live_frame")
        return jsonify(error="live preview error"), 500


@app.get("/live_stream.mjpg")
def live_stream():
    """
    MJPEG stream for browser live view.

    Usage in browser:
      <img src="/live_stream.mjpg">
    """
    def generate():
        global _live_mjpeg_proc

        # Turn Live View ON once for this stream
        #try:
        #    _set_liveview(True)
        #except Exception:
        #    pass

        proc = subprocess.Popen(
            ["gphoto2", "--capture-movie", "--stdout"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        # REGISTER proc globally so we can kill it from a separate endpoint
        with _live_mjpeg_lock:
            _live_mjpeg_proc = proc

        buf = b""
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk

                while True:
                    start = buf.find(b"\xff\xd8")  # SOI
                    if start == -1:
                        buf = buf[-3:]
                        break

                    end = buf.find(b"\xff\xd9", start + 2)  # EOI
                    if end == -1:
                        buf = buf[start:]
                        break

                    frame = buf[start:end + 2]
                    buf = buf[end + 2:]

                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" +
                        frame +
                        b"\r\n"
                    )
        finally:
            # CLEAR global reference
            with _live_mjpeg_lock:
                if _live_mjpeg_proc is proc:
                    _live_mjpeg_proc = None

            # Kill the movie process quickly; don't sit on a 3s wait
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=0.8)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass

            try:
                _set_liveview(False)
            except Exception:
                pass

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )

@app.post("/api/live_stream/stop")
def live_stream_stop():
    """
    Stop any running MJPEG /live_stream.mjpg gphoto2 process quickly.
    """
    global _live_mjpeg_proc
    with _live_mjpeg_lock:
        proc = _live_mjpeg_proc
        _live_mjpeg_proc = None

    if not proc:
        return jsonify(status="no stream"), 200

    try:
        proc.terminate()
        try:
            proc.wait(timeout=0.8)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass

    try:
        _set_liveview(False)
    except Exception:
        pass

    return jsonify(status="stopped"), 200



# ---------- run ----------
if __name__ == "__main__":
    # deps: sudo apt-get install -y exiftool python3-pil dcraw
    app.run(host="0.0.0.0", port=8090)
