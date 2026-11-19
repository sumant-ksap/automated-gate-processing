# Automated Gate Processing (Computer Vision)

AI-assisted truck gate for a Yard Management System (YMS). Cameras on every lane
read the truck, inspect the trailer and match it against the yard database. The
barrier then opens automatically, so a guard no longer checks paperwork for every
truck in the queue. Several lanes are processed at the same time.

| Capability | How it works |
|---|---|
| **OCR & license plate recognition** | [RapidOCR](https://github.com/RapidAI/RapidOCR) (PaddleOCR models on ONNX Runtime, CPU-only). A contour-based plate localizer finds the plate, crops it, enlarges it and reads it again. "USDOT"/"SEAL" labels are paired with the values printed next to them. |
| **Trailer numbers** | ISO 6346 parsing with **check-digit validation**. OCR glyph mix-ups (O/0, J/U, I/1, …) are repaired only when the check digit confirms the fix. |
| **Damage detection** | Trailer panels are segmented by region growing. Anomalies enclosed by a panel are classified as **dent / rust / hole / scratch** with a severity. A trained **YOLOv8 ONNX** model can be plugged in instead (`GATE_DAMAGE_MODEL`). |
| **Seal check** | The bolt-seal number read from the rear camera is compared with the ASN. A missing, broken or mismatched seal → security **HOLD**. |
| **Instant database matching** | Plate + trailer + USDOT are cross-referenced against carriers, **ASNs** and **dock appointments**. At least two independent identifiers must agree. Look-alike plate reads are matched fuzzily. The appointment window and duplicate entries are checked too. |
| **Automatic gate entry** | Decision → barrier command (simulated, or a webhook to a PLC/relay) → the appointment and ASN are marked arrived → the event is logged with annotated images. |
| **Multi-lane** | Each lane has its own cameras. Lanes are processed concurrently on a worker pool, with CPU cores split between the OCR engines. |

### Gate decisions

| Decision | Barrier | Typical cause |
|---|---|---|
| `GRANTED` | opens | identity, appointment, seal and trailer verified → dock door (or staging lot if early/late) |
| `INSPECTION` | opens | healthy paperwork but damage found → inspection bay (the load isn't stopped) |
| `HOLD` | closed | seal missing / broken / not matching the ASN |
| `MANUAL_REVIEW` | closed | unreadable IDs, trailer/tractor mismatch, duplicate arrival, outside the appointment window |
| `DENIED` | closed | no appointment, unknown or suspended carrier |

Guards handle the exceptions in the dashboard (**Exceptions** tab). Every override is
recorded with the guard's name and a note.

## Quick start (Windows)

```bat
run.bat
```

Or step by step:

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m gate serve --simulate   # dashboard at http://localhost:8000
```

`--simulate` starts the traffic simulator. It renders synthetic side and rear camera
frames for every lane and mixes in healthy trucks, damaged trailers, broken seals,
unscheduled trucks, suspended carriers, wrong trailers and unreadable (dirty-lens)
frames. The **Simulator decision accuracy** tile shows how often the pipeline made the
correct decision.

> **Note:** `opencv-python` is pinned to 4.10. On this machine, Windows Smart App
> Control / Application Control blocks the DLL of newer OpenCV wheels.

## Command line

```bash
python -m gate serve [--host 0.0.0.0] [--port 8000] [--simulate]
python -m gate process side.jpg rear.jpg --lane 1      # process your own images
python -m gate generate samples -n 10 [--scenario damaged]
python -m gate evaluate -n 90                          # end-to-end accuracy report
python -m gate seed --reset                            # reload demo carriers/ASNs/appointments
pytest                                                 # 28 tests
```

## Connecting real cameras

Set `GATE_LANE_SOURCES` (JSON) to choose how each lane gets its images. Lanes not
listed are fed by the simulator or the API.

```bash
set GATE_LANE_SOURCES={"1": {"type": "camera", "uris": ["rtsp://10.0.0.21/stream", "rtsp://10.0.0.22/stream"], "names": ["side", "rear"]}, "2": {"type": "folder", "path": "D:/gate/lane2"}}
```

* **camera**: OpenCV `VideoCapture` (RTSP/HTTP URL or USB index). Motion on the first
  camera arms the trigger. When the truck stops at the barrier, one frame from every
  camera is processed.
* **folder**: for existing LPR systems that FTP-upload snapshots. Files named
  `<pass>__<camera>.jpg` are grouped into one truck pass and moved to `done/`.
* **API**: `POST /api/lanes/{id}/process` with one or more image files.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `GATE_NUM_LANES` / `GATE_WORKERS` | 4 / 4 | lanes, and concurrent processing threads |
| `GATE_MIN_OCR_CONF` | 0.60 | minimum confidence to trust a read |
| `GATE_EARLY_MIN` / `GATE_LATE_MIN` / `GATE_ON_TIME_MIN` | 120 / 240 / 30 | appointment window in minutes (on-time → straight to the dock) |
| `GATE_DAMAGE_MODEL` / `GATE_DAMAGE_CLASSES` | – | YOLOv8 ONNX damage model and its class names |
| `GATE_DAMAGE_MIN_AREA` | 500 | smallest damage blob, in pixels |
| `GATE_CONTROLLER_URL` | – | POST `{"lane", "command": "OPEN"/"CLOSED"}` to a barrier PLC/relay |
| `GATE_SIM_INTERVAL` / `GATE_SIM_AUTOSTART` | 6 / false | simulator headway per lane, and whether it starts automatically |
| `GATE_DATA_DIR` | `./data` | SQLite DB and event images |

## REST API

| Method | Path | |
|---|---|---|
| GET | `/api/lanes` | lane status and barrier state |
| POST | `/api/lanes/{id}/process` | upload images (multipart `files`) → gate event |
| POST | `/api/lanes/{id}/simulate?scenario=` | run one simulated truck |
| GET/POST | `/api/simulator` | simulator state; start/stop and set the interval |
| GET | `/api/events`, `/api/events/{id}` | gate events with reads, damage, reasons and match |
| POST | `/api/events/{id}/override` | guard decision `{decision, by, note}` |
| GET | `/api/images/{name}` | raw or annotated camera frames |
| GET/POST | `/api/appointments` | dock appointments; creating one also creates its ASN |
| GET | `/api/asns`, `/api/carriers`, `/api/stats` | yard data and KPIs |
| WS | `/ws` | live events, lane and barrier updates |

Interactive docs: `http://localhost:8000/docs`.

## Project layout

```
gate/
  vision/ocr.py          RapidOCR wrapper (one engine per worker thread)
  vision/identifiers.py  USDOT / ISO 6346 / plate / seal parsing, check digits, OCR repair
  vision/reader.py       plate localization, label pairing, multi-camera fusion
  vision/damage.py       panel-anomaly damage detector + YOLO ONNX detector
  vision/annotate.py     draws reads / damage / decision on the frames
  matching.py            ASN + appointment matching and the decision engine
  pipeline.py            frames → event (+ barrier, DB updates, images)
  lanes.py               multi-lane manager: simulator, camera, folder sources
  gatecontrol.py         barrier controllers (simulated / webhook)
  api.py, static/        FastAPI server and operator dashboard
  sim/                   synthetic truck renderer and traffic scenarios
  db.py, seed.py         SQLite schema and demo data
tests/                   unit, pipeline and API tests
```

## Going to production

The classical damage detector works with no training data, but real trailers vary far
more than the simulator does. Collect frames from your own gates (every event stores
the raw images), label them, train a YOLOv8 model, export it to ONNX, and set
`GATE_DAMAGE_MODEL`. The OCR engine works on real photos as-is. Tune
`GATE_MIN_OCR_CONF` using the confidences recorded in each event.
