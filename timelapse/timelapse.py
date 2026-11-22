#!/usr/bin/env python3
"""
Per-print debug timelapse for Prusa printers (PrusaLink API).

Polls PrusaLink, grabs a camera frame every INTERVAL seconds while a job is
running, burns a timestamp/status overlay into each frame, and when the job
ends (FINISHED / STOPPED / ERROR / IDLE / ...) encodes the frames into an H.264 mp4.

All configuration comes from environment variables, see timelapse/README.md.
"""
import datetime
import io
import logging
import os
import re
import shutil
import subprocess
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
OUT = os.environ.get("OUTPUT_DIR", "/data")
KEEP_FRAMES = os.environ.get("KEEP_FRAMES", "false").lower() == "true"
RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "0"))  # 0 = keep forever

ACTIVE = {"PRINTING", "PAUSED", "ATTENTION"}
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("timelapse")

if not (RTSP or SNAPSHOT):
    raise SystemExit("set CAMERA_RTSP or CAMERA_SNAPSHOT_URL")

BASE = HOST if HOST.startswith(("http://", "https://")) else f"http://{HOST}"
api = requests.Session()
api.auth = HTTPDigestAuth(USER, PASSWORD)


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


def render(session, final_state):
    try:
        if session["n"] < 2:
            log.info("only %d frame(s), skipping video", session["n"])
            return
        out = os.path.join(OUT, f"{session['slug']}_{final_state}.mp4")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", FPS,
               "-i", os.path.join(session["dir"], "%06d.jpg"),
               # even dimensions + limited-range yuv420p (jpeg input is full-range
               # yuvj420p, which some players handle badly)
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2:out_range=tv,format=yuv420p",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", CRF,
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
        log.info("encoding %d frames -> %s", session["n"], out)
        subprocess.run(cmd, check=True)
    except Exception as e:
        # keep the frames so the video can be re-encoded by hand
        log.error("encoding failed, keeping frames in %s: %s", session["dir"], e)
        return
    finally:
        cleanup_old()
    if not KEEP_FRAMES:
        shutil.rmtree(session["dir"], ignore_errors=True)


def cleanup_old():
    if RETENTION_DAYS <= 0:
        return
    cutoff = time.time() - RETENTION_DAYS * 86400
    for entry in os.scandir(OUT):
        if entry.is_file() and entry.name.endswith(".mp4") and entry.stat().st_mtime < cutoff:
            log.info("retention: removing %s", entry.name)
            os.remove(entry.path)


def start_session(job_id):
    name = "print"
    try:
        name = get("/api/v1/job").get("file", {}).get("display_name") or name
    except Exception as e:
        log.warning("could not read job name: %s", e)
    name = re.sub(r"\.b?gcode$", "", name)
    slug = f"{datetime.datetime.now():%Y%m%d-%H%M%S}_" + re.sub(r"[^\w.-]+", "_", name)[:60]
    d = os.path.join(OUT, "frames", slug)
    os.makedirs(d, exist_ok=True)
    log.info("job %s started: %s", job_id, name)
    return {"id": job_id, "name": name, "slug": slug, "dir": d, "n": 0}


def main():
    os.makedirs(OUT, exist_ok=True)
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
                        f"#{session['n'] + 1}  {session['name']}")
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
