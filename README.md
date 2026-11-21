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
| **Dynamic ETA** | GPS pings from truck telematics, plus traffic and weather along the remaining route, feed a machine-learning model that predicts each truck's arrival time and an earliest–latest window. The model retrains itself from every truck that actually arrives. |
| **Proactive staging** | Trucks within 30 minutes of arrival, or part of a predicted **arrival surge**, are pre-checked: operating authority, insurance, appointment, ASN and seal, trailer number, dock-door conflicts. Each gets a digital check-in packet, so the gate only confirms identity. |
| **Dock & spot allocation** | Trucks that clear the gate are assigned a dock door or a yard parking spot **jointly**, by an optimal assignment (Hungarian algorithm) that weighs load priority (perishable, urgent cross-dock, detention clock), equipment compatibility (cold docks, ground-level flatbed doors, powered reefer spots) and yard congestion (travel distance, per-zone traffic). When a door frees up, the most urgent waiting trailer moves in. |
| **Driver communication** | Every gate and yard decision is sent to the driver's phone in their language (English, Spanish, Hindi, Punjabi, Russian): door or spot with turn-by-turn yard directions, "your door is ready", inspection, hold, refusal, unloading done, pre-check result. Sent by SMS (simulated, or Twilio) with a link to a live phone page that shows the route on a yard map. |

### Camera layout (per lane)

| Camera | Reads | Damage inspection |
|---|---|---|
| **front** (LPR) | license plate | no (it sees the cab, not the trailer) |
| **side** | USDOT number, trailer number | trailer side panel |
| **rear** | trailer number, bolt-seal number | rear doors |

Name cameras `front`, `side` and `rear` (`names` in `GATE_LANE_SOURCES`, or the `__front` / `__side` / `__rear`
suffix on uploaded file names) so that only the trailer cameras are used for damage inspection.

### Gate decisions

| Decision | Barrier | Typical cause |
|---|---|---|
| `GRANTED` | opens | identity, appointment, seal and trailer verified → dock door (or staging lot if early/late) |
| `INSPECTION` | opens | healthy paperwork but damage found, or a trailer camera too blurred to inspect → inspection bay (the load isn't stopped) |
| `HOLD` | closed | seal missing / broken / not matching the ASN |
| `MANUAL_REVIEW` | closed | unreadable or only one ID read, trailer/tractor mismatch, duplicate arrival, missed appointment (NO-SHOW) |
| `DENIED` | closed | no appointment, unknown or suspended carrier, expired insurance |

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

`--simulate` starts both simulators. The gate simulator renders synthetic front, side and rear camera
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
python -m gate verify                                  # acceptance check of all 4 feature areas (~3 min)
pytest                                                 # 53 tests
```

## Connecting real cameras

Set `GATE_LANE_SOURCES` (JSON) to choose how each lane gets its images. Lanes not
listed are fed by the simulator or the API.

```bash
set GATE_LANE_SOURCES={"1": {"type": "camera", "uris": ["rtsp://10.0.0.20/stream", "rtsp://10.0.0.21/stream", "rtsp://10.0.0.22/stream"], "names": ["front", "side", "rear"]}, "2": {"type": "folder", "path": "D:/gate/lane2"}}
```

* **camera**: OpenCV `VideoCapture` (RTSP/HTTP URL or USB index). Motion on the first
  camera arms the trigger. When the truck stops at the barrier, one frame from every
  camera is processed.
* **folder**: for existing LPR systems that FTP-upload snapshots. Files named
  `<pass>__<camera>.jpg` are grouped into one truck pass and moved to `done/`.
* **API**: `POST /api/lanes/{id}/process` with one or more image files.

## Predictive arrival & pre-check-in

Open the **Inbound & pre-check** tab (or go straight to `http://localhost:8000/#inbound`).

* **Approach map:** trucks within 45 km of the facility, coloured by pre-check status, with traffic incidents and
  rain/storm cells. Click a truck to see its check-in packet and prediction history.
* **Inbound board:** each truck's predicted arrival with its window (P10–P90), the static distance ÷ speed ETA
  for comparison, minutes early or late against its slot, and the conditions on its route.
* **Arrival surge alerts:** shown when 6 or more trucks are predicted within 10 minutes, comparing the lanes and
  dwell time needed with manual check-in against pre-cleared check-in. **Send convoy (surge)** triggers one on
  demand.
* **ETA model card:** the model's average error against the static ETA, on held-out trips and live on arrived
  trucks.

**How the ETA model works.** A ridge regression learns `log(actual travel time / static travel time)` from 14
signals a real system can observe: remaining distance, GPS speed over the last 5 minutes, traffic index here and
along the route, rain/snow/wind along the route, and time of day. The earliest–latest window comes from the
model's own out-of-sample errors. It starts from 500 simulated historical trips and is retrained after every 5
real arrivals. On held-out trips it averages **≈1.5 min error vs ≈4 min** for a static ETA.

**Connecting real telematics.** Point your ELD/telematics provider's webhook (Samsara, Motive, Geotab …), or a
small adapter, at:

```
POST /api/telematics/pings
[{"vehicle_id": "ELD-77", "plate": "TX12345", "lat": 32.91, "lon": -96.70, "speed_kph": 72,
  "timestamp": "2026-11-20T15:04:00Z"}]
```

Trucks are linked to appointments by `appointment_ref`, `vehicle_id` or `plate`. Traffic and weather are
**simulated** by default. Set `GATE_WEATHER=openmeteo` for live weather from Open-Meteo (free, no key). A
commercial traffic feed (TomTom, HERE, Google) plugs into `gate/predictive/conditions.py` and has not been built
or tested yet.

## Intelligent dock & spot allocation

Open the **Yard & docks** tab (`http://localhost:8000/#yard`).

* **Yard model:** 24 doors (D01–D04 cold docks, D05–D06 ground-level ramps for flatbeds, D07–D20 dry docks,
  D21–D24 cross-dock) and 60 parking spots in 3 rows; P-A01–P-A08 have shore power for reefer trailers.
* **Load data** comes from the ASN: `trailer_type` (DRY_VAN / REEFER / FLATBED), `load_class`
  (STANDARD / PERISHABLE / CROSS_DOCK) and, for cross-dock freight, `due_by` (outbound departure).
  Detention charges start 120 min after the appointment slot.
* **Priority score:** 1 + 3 for perishable, + 2 for cross-dock (+ 2 more if the outbound leaves within 90 min),
  + 2 / + 1 if detention starts within 30 / 60 min.
* **Hard rules:** perishables only at cold docks and on powered spots; flatbeds only at ground-level doors;
  van trailers never at ground-level doors. **Soft rules:** keep cold and cross-dock doors for the loads that need
  them; prefer the booked door; minimise travel; spread trucks across zones (re-solve if one batch would send
  more than 3 trucks into the same zone).
* **Simultaneous check-in** sends 5–10 trucks through at once and compares the AI plan with a
  first-come-first-served dispatcher on the same yard. Over 12 simulated batches of 8 trucks in a busy yard, it
  made **0 equipment mistakes vs 30**, sent **80 vs 66** trucks straight to a suitable door, and drove about 4.5%
  less.
* Unloading runs `GATE_YARD_TIME_SCALE` (default 10) times faster than real time so the yard turns over during a
  demo. Set it to `1` for real time.

## Automated driver communication

Open the **Driver messages** tab (`http://localhost:8000/#drivers`). Click a message to see it as the driver
receives it, then **Open the driver's phone page**.

* **When messages are sent:** pre-check result before arrival, gate decision (door / spot / inspection / hold /
  guard check / refused), "your door is ready" when a waiting trailer is moved to a door, and "unloading
  finished". A message is only sent when the truck is matched to an appointment, because that's where the phone
  number comes from.
* **Driver data:** each appointment carries `driver_phone` (E.164) and `driver_language` (`en`, `es`, `hi`, `pa`,
  `ru`), normally sent by the carrier or TMS with the booking. The demo data uses fictional 555-01xx numbers.
* **Directions** are computed from the yard layout: main road north from the gate, the apron in front of the
  doors, and a lane south of each parking row. For example: drive north 230 m, turn left, continue 80 m, back into
  door D07. Spots say which side they're on and remind reefer drivers to plug in.
* **Driver phone page** (`/driver.html?t=<token>`, linked in every SMS; no app to install): current instruction
  in large text, numbered steps, a yard map with the route and destination, a language picker (re-sends the
  instruction in the new language), and an **OK** button to confirm.
* **Replies by SMS** (webhook `POST /api/sms/inbound`, Twilio format): `OK` confirms, `EN` / `ES` / `HI` / `PA` /
  `RU` switches language, and `HELP` raises an alert on the dashboard.
* **Real SMS:** set `GATE_SMS_PROVIDER=twilio` with `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_FROM`,
  set `GATE_PUBLIC_URL` to an address drivers' phones can reach, and point the Twilio number's incoming-message
  webhook at `/api/sms/inbound` (signatures are verified). **Not tested against the live Twilio service.**
* **Translations** were written for this prototype. Have native speakers from your driver community review them
  before going live.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `GATE_NUM_LANES` / `GATE_WORKERS` | 4 / 4 | lanes, and concurrent processing threads |
| `GATE_MIN_OCR_CONF` | 0.60 | minimum confidence to trust a read |
| `GATE_EARLY_MIN` / `GATE_LATE_MIN` / `GATE_ON_TIME_MIN` | 120 / 240 / 30 | appointment window in minutes (on-time → straight to the dock) |
| `GATE_DAMAGE_MODEL` / `GATE_DAMAGE_CLASSES` | – | YOLOv8 ONNX damage model and its class names |
| `GATE_DAMAGE_MIN_AREA` | 500 | smallest damage blob, in pixels |
| `GATE_MIN_SHARPNESS` | 20 | side/rear frames below this sharpness aren't trusted for damage; the truck goes to inspection. Calibrate per camera from `image_sharpness` in event details |
| `GATE_CONTROLLER_URL` | – | POST `{"lane", "command": "OPEN"/"CLOSED"}` to a barrier PLC/relay |
| `GATE_SIM_INTERVAL` / `GATE_SIM_AUTOSTART` | 6 / false | simulator headway per lane, and whether it starts automatically |
| `GATE_DATA_DIR` | `./data` | SQLite DB and event images |
| `GATE_FACILITY_NAME` / `_LAT` / `_LON` | DC-01 Dallas | facility location for ETA prediction |
| `GATE_WEATHER` | `sim` | `openmeteo` for live weather |
| `GATE_PRECHECK_HORIZON_MIN` | 30 | pre-check trucks this many minutes before arrival |
| `GATE_SURGE_WINDOW_MIN` / `GATE_SURGE_THRESHOLD` | 10 / 6 | surge = this many trucks predicted within the window |
| `GATE_FLEET_AUTOSTART` / `GATE_FLEET_TARGET` | false / 10 | inbound fleet simulator, and how many trucks it keeps on the road |
| `GATE_YARD_TIME_SCALE` | 10 | unloading speed-up for demos (1 = real time) |
| `GATE_SMS_PROVIDER` | `sim` | `twilio` for real SMS (plus `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`) |
| `GATE_PUBLIC_URL` | `http://localhost:8000` | base address used in the driver links inside SMS messages |

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
| GET | `/api/inbound` | inbound trucks with ETA windows, surge alerts, conditions, model metrics |
| POST | `/api/telematics/pings` | GPS pings from truck telematics |
| GET | `/api/inbound/trips/{id}` | one inbound truck: check-in packet and prediction history |
| GET/POST | `/api/inbound/model`, `/api/inbound/model/retrain` | ETA model metrics; retrain |
| GET/POST | `/api/fleet-sim`, `/api/fleet-sim/convoy` | inbound fleet simulator on/off; send a convoy |
| GET | `/api/yard` | doors, spots, trailers in the yard, moves, zone congestion, last batch comparison |
| POST | `/api/yard/simultaneous-checkin?n=8` | demo: n trucks check in at once (AI plan vs first-come-first-served) |
| GET | `/api/yard/visits/{id}` | one trailer's yard visit: why it was placed there, its moves |
| GET | `/api/messages` | driver messages (SMS / app) with delivery and confirmation status |
| GET | `/api/driver/{token}` | data for the driver's phone page |
| POST | `/api/driver/{token}/ack`, `/api/driver/{token}/language` | driver confirms; driver changes language |
| POST | `/api/sms/inbound` | incoming SMS webhook (Twilio format) |
| WS | `/ws` | live events, lane, barrier, inbound, yard and message updates |

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
  api.py, static/        FastAPI server, operator dashboard, driver phone page (static/driver.html)
  sim/                   synthetic truck renderer and traffic scenarios
  predictive/            dynamic ETA + pre-check-in: conditions (traffic/weather), world (truck motion),
                         eta_model (ML), precheck (packets, surge detection), service (telematics, fleet sim)
  yard/allocator.py      dock & spot allocation: yard layout, priority, equipment rules, Hungarian assignment
  comms/                 driver communication: i18n (5 languages), directions, notifier (SMS / Twilio)
  db.py, seed.py         SQLite schema and demo data
tests/                   unit, pipeline and API tests
```

## Going to production

The classical damage detector works with no training data, but real trailers vary far
more than the simulator does. Collect frames from your own gates (every event stores
the raw images), label them, train a YOLOv8 model, export it to ONNX, and set
`GATE_DAMAGE_MODEL`. The OCR engine works on real photos as-is. Tune
`GATE_MIN_OCR_CONF` using the confidences recorded in each event.
