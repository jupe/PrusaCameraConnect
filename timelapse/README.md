# Print timelapse sidecar

A small, separate container that records a **per-print debug timelapse**.
PrusaCameraConnect only relays snapshots to Prusa Connect and stores nothing;
this sidecar reads the same camera and keeps a video of every print.

For every print it:

1. polls PrusaLink `GET /api/v1/status` every `INTERVAL` seconds (digest auth),
2. grabs a camera frame (RTSP via ffmpeg, or an HTTP snapshot URL),
3. burns an overlay into the frame: time, state, progress, Z height, nozzle/bed temps, frame #, job name,
4. when the print ends, encodes `/data/<YYYYmmdd-HHMMSS>_<jobname>_<FINAL_STATE>.mp4` (H.264).

Useful for debugging parts that break loose from the bed mid-print.

Sessions: `PRINTING`, `PAUSED`, `ATTENTION` are active. Any other state
(`FINISHED`, `STOPPED`, `ERROR`, `IDLE`, ...) ends the session, and so does a new `job.id`
(suffix `NEXTJOB`). Network and camera errors only skip that tick, so they don't end the session.

## Setup

```bash
cd timelapse
cp .env.example .env      # fill in printer IP, PrusaLink password, camera URL
docker compose up -d --build
docker compose logs -f print-timelapse
# videos appear in ./timelapses/
```

`.env` and `timelapses/` are gitignored. Keep your credentials and addresses there.

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `PRUSALINK_HOST` | (required) | printer IP/hostname (or full `http://...` base URL) |
| `PRUSALINK_USER` | `maker` | |
| `PRUSALINK_PASSWORD` | (required) | printer: Settings > Network > PrusaLink |
| `CAMERA_RTSP` | | `rtsp://...`, the same source as in your prusacameraconnect config |
| `CAMERA_SNAPSHOT_URL` | | HTTP jpeg, an alternative to RTSP (wins if both are set) |
| `INTERVAL` | `5` | seconds between frames; 2–3 is better for catching collisions |
| `FPS` | `25` | output video fps |
| `CRF` | `28` | higher = smaller file |
| `OUTPUT_DIR` | `/data` | |
| `KEEP_FRAMES` | `false` | keep jpgs after encoding |
| `RETENTION_DAYS` | `0` | delete videos older than N days after each encode; `0` = keep forever |
| `TZ` | `Europe/Helsinki` | timezone for the overlay and file names |

## Verifying on your setup

```bash
# 1. PrusaLink field names (printer.state, printer.axis_z, job.id, job.progress)
curl --digest -u maker:PASS http://PRINTER_IP/api/v1/status
curl --digest -u maker:PASS http://PRINTER_IP/api/v1/job      # file.display_name

# 2. single frame grab from the camera
ffmpeg -rtsp_transport tcp -i 'rtsp://...' -frames:v 1 test.jpg

# 3. run a short print and check that an mp4 appears in ./timelapses/
```

Notes:
- If the container restarts mid-print, the frames already captured stay in
  `timelapses/frames/<slug>/` and a new session starts for the rest of the print.
- If encoding fails, the frames are kept so you can encode them by hand.
