# Print timelapse sidecar

A small, separate container that records a **per-print debug timelapse**.
PrusaCameraConnect only relays snapshots to Prusa Connect and stores nothing;
this sidecar reads the same camera and keeps a video of every print.

For every print it:

1. polls PrusaLink `GET /api/v1/status` every `INTERVAL` seconds (digest auth),
2. grabs a camera frame (RTSP via ffmpeg, or an HTTP snapshot URL),
3. burns an overlay into the frame: time, state, progress, Z height, nozzle/bed temps, frame #, job name,
4. when the print ends, encodes `/data/<YYYYmmdd-HHMMSS>_<jobname>_<FINAL_STATE>.mp4` (H.264),
   saves the last frame as `<...>_<FINAL_STATE>.jpg` + a 480 px `<...>_thumb.jpg`, and prepends the
   print to `/data/history.json`,
5. serves `/data` read-only over HTTP (default port 8099), e.g. for a Home Assistant dashboard.

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
| `RETENTION_DAYS` | `0` | after each print, delete mp4/jpg files older than N days and drop their history entries; `0` = keep forever |
| `HISTORY_MAX` | `50` | max entries kept in `history.json` |
| `HTTP_PORT` | `8099` | read-only HTTP server port; `0` disables it |
| `TZ` | `Europe/Helsinki` | timezone for the overlay and file names |

## Print history over HTTP

`http://<host>:8099/history.json` (served with `Cache-Control: no-cache`, newest first):

```json
{
  "count": 1,
  "prints": [
    {
      "name": "benchy_0.2mm_PLA_MK4S_45m",
      "state": "FINISHED",
      "start": "2026-11-22T10:07:35+02:00",
      "end": "2026-11-22T10:33:13+02:00",
      "duration_min": 26,
      "frames": 312,
      "video": "20261122-100735_benchy_0.2mm_PLA_MK4S_45m_FINISHED.mp4",
      "image": "20261122-100735_benchy_0.2mm_PLA_MK4S_45m_FINISHED.jpg",
      "thumb": "20261122-100735_benchy_0.2mm_PLA_MK4S_45m_FINISHED_thumb.jpg"
    }
  ]
}
```

File names are relative to the server root: `http://<host>:8099/<video>`. `video` is `null` when the
print had fewer than 2 frames, or when encoding failed (the frames are kept then).
Videos support HTTP Range requests, so they play in Safari / the iOS Home Assistant app.
The in-progress `frames/` folder is not served (404).

There is no authentication: only expose the port on your LAN.

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
