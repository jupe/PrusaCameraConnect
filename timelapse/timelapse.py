#!/usr/bin/env python3
"""
Per-print debug timelapse for Prusa printers (PrusaLink API).

Polls PrusaLink, grabs a camera frame every INTERVAL seconds while a job is
running, burns a timestamp/status overlay into each frame, and when the job
ends (FINISHED / STOPPED / ERROR / IDLE / ...) encodes the frames into an H.264 mp4,
saves the last frame as a still + thumbnail and records the print in history.json.

OUTPUT_DIR (minus the in-progress frames/) is served read-only over HTTP on HTTP_PORT,
e.g. for a Home Assistant dashboard.

All configuration comes from environment variables, see timelapse/README.md.
"""
import datetime
import http.server
import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time

import requests
from PIL import Image, ImageDraw, ImageFont
from requests.auth import HTTPDigestAuth

HOST = os.environ["PRUSALINK_HOST"]
USER = os.environ.get("PRUSALINK_USER", "maker")
PASSWORD = os.environ["PRUSALINK_PASSWORD"]
RTSP = os.environ.get("CAMERA_RTSP")
SNAPSHOT = os.environ.get("CAMERA_SNAPSHOT_URL")
INTERVAL = float(os.environ.get("INTERVAL", "5"))
FPS = os.environ.get("FPS", "25")
CRF = os.environ.get("CRF", "28")
OUT = os.path.abspath(os.environ.get("OUTPUT_DIR", "/data"))
KEEP_FRAMES = os.environ.get("KEEP_FRAMES", "false").lower() == "true"
RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "0"))  # 0 = keep forever
HISTORY_MAX = int(os.environ.get("HISTORY_MAX", "50"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8099"))  # 0 = no HTTP server

ACTIVE = {"PRINTING", "PAUSED", "ATTENTION"}
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FRAMES = os.path.join(OUT, "frames")
HISTORY = os.path.join(OUT, "history.json")
THUMB_WIDTH = 480

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("timelapse")

if not (RTSP or SNAPSHOT):
    raise SystemExit("set CAMERA_RTSP or CAMERA_SNAPSHOT_URL")

BASE = HOST if HOST.startswith(("http://", "https://")) else f"http://{HOST}"
api = requests.Session()
api.auth = HTTPDigestAuth(USER, PASSWORD)


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def get(path):
    r = api.get(f"{BASE}{path}", timeout=5)
    r.raise_for_status()
    # /api/v1/job returns 204 No Content when there is no job
    return r.json() if r.status_code == 200 and r.content else {}


def grab_frame() -> bytes:
    if SNAPSHOT:
        r = requests.get(SNAPSHOT, timeout=10)
        r.raise_for_status()
        return r.content
    cmd = ["ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", RTSP,
           "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "-"]
    data = subprocess.run(cmd, capture_output=True, timeout=20, check=True).stdout
    if not data:
        raise RuntimeError("ffmpeg returned no frame")
    return data


def font(size):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default(size=size)


def overlay(jpeg: bytes, text: str) -> Image.Image:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    size = max(14, img.height // 32)
    f = font(size)
    d = ImageDraw.Draw(img)
    pad = size // 3
    _, _, r, b = d.textbbox((pad, pad), text, font=f)
    d.rectangle((0, 0, max(img.width, r + pad), b + pad), fill=(0, 0, 0))
    d.text((pad, pad), text, font=f, fill=(255, 255, 255))
    return img


def num(v, fmt):
    return format(v, fmt) if isinstance(v, (int, float)) else "?"


# --- history -----------------------------------------------------------------

def write_json_atomic(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def load_history():
    try:
        with open(HISTORY) as fh:
            return json.load(fh).get("prints", [])
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning("history.json unreadable, starting a new one: %s", e)
        return []


def save_history(prints):
    write_json_atomic(HISTORY, {"count": len(prints), "prints": prints})


def apply_retention(prints):
    """Delete mp4/jpg files older than RETENTION_DAYS and drop their history entries."""
    if RETENTION_DAYS <= 0:
        return prints
    cutoff = time.time() - RETENTION_DAYS * 86400
    for entry in os.scandir(OUT):
        if (entry.is_file() and entry.name.endswith((".mp4", ".jpg"))
                and entry.stat().st_mtime < cutoff):
            log.info("retention: removing %s", entry.name)
            os.remove(entry.path)

    def expired(p):
        try:
            return datetime.datetime.fromisoformat(p["end"]).timestamp() < cutoff
        except Exception:
            return False
    return [p for p in prints if not expired(p)]


def record(entry):
    prints = [entry] + load_history()
    prints = apply_retention(prints)[:HISTORY_MAX]
    save_history(prints)


# --- session -----------------------------------------------------------------

def render(session, final_state):
    n = session["n"]
    if n == 0:
        log.info("no frames captured, nothing to record")
        shutil.rmtree(session["dir"], ignore_errors=True)
        return

    base = f"{session['slug']}_{final_state}"
    end = now_iso()
    start_dt = datetime.datetime.fromisoformat(session["start"])
    entry = {
        "name": session["name"],
        "state": final_state,
        "start": session["start"],
        "end": end,
        "duration_min": round(
            (datetime.datetime.fromisoformat(end) - start_dt).total_seconds() / 60),
        "frames": n,
        "video": None,
        "image": None,
        "thumb": None,
    }

    # still of how the print ended (last frame, overlay included) + small thumbnail
    try:
        last = os.path.join(session["dir"], f"{n:06d}.jpg")
        shutil.copyfile(last, os.path.join(OUT, f"{base}.jpg"))
        entry["image"] = f"{base}.jpg"
        with Image.open(last) as img:
            img.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 10))
            img.save(os.path.join(OUT, f"{base}_thumb.jpg"), quality=80)
        entry["thumb"] = f"{base}_thumb.jpg"
    except Exception as e:
        log.error("could not write final image/thumbnail: %s", e)

    encoded = False
    if n < 2:
        log.info("only %d frame(s), skipping video", n)
    else:
        out = os.path.join(OUT, f"{base}.mp4")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", FPS,
               "-i", os.path.join(session["dir"], "%06d.jpg"),
               # even dimensions + limited-range yuv420p (jpeg input is full-range
               # yuvj420p, which some players handle badly)
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2:out_range=tv,format=yuv420p",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", CRF,
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
        log.info("encoding %d frames -> %s", n, out)
        try:
            subprocess.run(cmd, check=True)
            entry["video"] = f"{base}.mp4"
            encoded = True
        except Exception as e:
            # keep the frames so the video can be re-encoded by hand
            log.error("encoding failed, keeping frames in %s: %s", session["dir"], e)

    try:
        record(entry)
    except Exception as e:
        log.error("could not update history.json: %s", e)

    if (encoded or n < 2) and not KEEP_FRAMES:
        shutil.rmtree(session["dir"], ignore_errors=True)


def start_session(job_id):
    name = "print"
    try:
        name = get("/api/v1/job").get("file", {}).get("display_name") or name
    except Exception as e:
        log.warning("could not read job name: %s", e)
    name = re.sub(r"\.b?gcode$", "", name)
    slug = f"{datetime.datetime.now():%Y%m%d-%H%M%S}_" + re.sub(r"[^\w.-]+", "_", name)[:60]
    d = os.path.join(FRAMES, slug)
    os.makedirs(d, exist_ok=True)
    log.info("job %s started: %s", job_id, name)
    return {"id": job_id, "name": name, "slug": slug, "dir": d, "n": 0, "start": now_iso()}


# --- HTTP --------------------------------------------------------------------

class Handler(http.server.SimpleHTTPRequestHandler):
    """Read-only file server for OUTPUT_DIR with frames/ hidden and Range support
    (Safari / iOS refuse to play <video> from servers without byte ranges)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=OUT, **kwargs)

    def _hidden(self):
        p = os.path.realpath(self.translate_path(self.path))
        return p == FRAMES or p.startswith(FRAMES + os.sep) or p.endswith(".tmp")

    def send_head(self):
        self._range = None
        if self._hidden():
            self.send_error(404)
            return None
        path = self.translate_path(self.path)
        rng = self.headers.get("Range")
        if not rng or not os.path.isfile(path):
            return super().send_head()
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        size = os.path.getsize(path)
        if not m or m.groups() == ("", ""):
            return super().send_head()
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        else:  # suffix range: last N bytes
            start, end = max(0, size - int(m.group(2))), size - 1
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        f = open(path, "rb")
        f.seek(start)
        self._range = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(self._range))
        self.send_header("Last-Modified", self.date_time_string(os.path.getmtime(path)))
        self.end_headers()
        return f

    def copyfile(self, source, outputfile):
        if self._range is None:
            return super().copyfile(source, outputfile)
        left = self._range
        while left > 0:
            buf = source.read(min(64 * 1024, left))
            if not buf:
                break
            outputfile.write(buf)
            left -= len(buf)

    def end_headers(self):
        if self.path.split("?", 1)[0].endswith("history.json"):
            self.send_header("Cache-Control", "no-cache")
        if self.command in ("GET", "HEAD"):
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def log_message(self, fmt, *args):
        log.debug("http %s " + fmt, self.address_string(), *args)


def start_http():
    srv = http.server.ThreadingHTTPServer(("", HTTP_PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
    log.info("serving %s on :%d", OUT, HTTP_PORT)


# --- main --------------------------------------------------------------------

def main():
    os.makedirs(FRAMES, exist_ok=True)
    if not os.path.exists(HISTORY):
        save_history([])
    if HTTP_PORT:
        start_http()
    log.info("watching %s every %ss (camera: %s)", BASE, INTERVAL, "snapshot" if SNAPSHOT else "rtsp")
    session = None
    while True:
        t0 = time.monotonic()
        try:
            st = get("/api/v1/status")
            printer = st.get("printer", {})
            job = st.get("job") or {}
            state = printer.get("state", "UNKNOWN")
            job_id = job.get("id")

            if session and (state not in ACTIVE or job_id != session["id"]):
                s, session = session, None
                render(s, state if state not in ACTIVE else "NEXTJOB")

            if state in ACTIVE and job_id is not None:
                if not session:
                    session = start_session(job_id)
                now = datetime.datetime.now()
                text = (f"{now:%Y-%m-%d %H:%M:%S}  {state}  "
                        f"{num(job.get('progress'), '.0f')}%  "
                        f"Z={num(printer.get('axis_z'), '.2f')}  "
                        f"N={num(printer.get('temp_nozzle'), '.0f')}C "
                        f"B={num(printer.get('temp_bed'), '.0f')}C  "
                        f"#{session['n'] + 1}\n{session['name']}")
                img = overlay(grab_frame(), text)
                # only advance the counter after a successful grab: ffmpeg's
                # image2 demuxer stops at the first gap in the sequence
                img.save(os.path.join(session["dir"], f"{session['n'] + 1:06d}.jpg"), quality=85)
                session["n"] += 1
        except Exception as e:
            # network/camera glitch: don't end the session, just skip this tick
            log.warning("tick failed: %s", e)
        time.sleep(max(0.0, INTERVAL - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
