"""Command line interface.

  python -m gate serve [--host 0.0.0.0] [--port 8000] [--simulate]
  python -m gate process side.jpg rear.jpg [--lane 1]
  python -m gate generate OUT_DIR [-n 10] [--scenario damaged]
  python -m gate evaluate [-n 45]
  python -m gate verify
  python -m gate seed [--reset]
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")


def cmd_serve(a) -> None:
    import uvicorn
    from .config import settings
    if a.simulate:
        settings.sim_autostart = True
        settings.fleet_autostart = True
    from .api import create_app
    print(f"\n  Gate dashboard: http://{'localhost' if a.host in ('0.0.0.0', '127.0.0.1') else a.host}:{a.port}\n")
    uvicorn.run(create_app(settings), host=a.host, port=a.port, log_level="warning")


def _pipeline():
    from .config import settings
    from .db import Database
    from .gatecontrol import SimulatedGateController
    from .pipeline import GatePipeline
    from .seed import seed_demo
    settings.ensure_dirs()
    db = Database(settings.db_path)
    if db.one("SELECT COUNT(*) AS n FROM carriers")["n"] == 0:
        seed_demo(db)
    from .seed import ensure_carriers
    ensure_carriers(db)
    return db, GatePipeline(db, settings, SimulatedGateController(auto_close_s=0.1))


def cmd_process(a) -> None:
    import cv2
    _, pipe = _pipeline()
    frames, names = [], []
    for p in a.images:
        img = cv2.imread(p)
        if img is None:
            sys.exit(f"cannot read image {p}")
        frames.append(img)
        names.append(Path(p).stem.split("__")[-1][:20])
    ev = pipe.process(a.lane, frames, names, {"source": "cli"})
    out = {k: ev[k] for k in ("id", "decision", "destination", "plate", "trailer_number", "dot_number",
                              "seal_number", "carrier_name", "processing_ms")}
    out["reasons"] = [r["message"] for r in ev["reasons"]]
    out["damage"] = [f"{d['severity']} {d['kind']} ({d['confidence']:.2f})" for d in ev["damage"]]
    out["annotated_images"] = [str(pipe.settings.image_dir / i["annotated"]) for i in ev["images"]]
    print(json.dumps(out, indent=2))


def cmd_generate(a) -> None:
    import cv2
    from .sim.scenarios import build_scenario
    from .sim.synth import render_truck
    db, _ = _pipeline()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)
    for i in range(a.n):
        name, spec = build_scenario(db, rng, a.scenario)
        frames = render_truck(spec, rng.randint(0, 2**31))
        for cam, img in frames.items():
            cv2.imwrite(str(out / f"truck{i:03d}_{name}__{cam}.jpg"), img)
        print(f"truck{i:03d}: {name:18} plate={spec.plate} trailer={spec.trailer_number} seal={spec.seal_number}")
    first = sorted(out.glob("truck000_*__front.jpg"))
    stem = first[0].name.replace("__front.jpg", "") if first else "truck000_<scenario>"
    print(f"\nWrote {a.n * 4} images (front / left / right / rear camera) to {out}. Process one truck with:\n"
          f"  python -m gate process {out / (stem + '__front.jpg')} {out / (stem + '__left.jpg')} "
          f"{out / (stem + '__right.jpg')} {out / (stem + '__rear.jpg')}")


def cmd_evaluate(a) -> None:
    """Run every simulator scenario through the pipeline and report accuracy."""
    import tempfile
    from collections import Counter
    from .config import Settings
    from .db import Database
    from .gatecontrol import SimulatedGateController
    from .pipeline import GatePipeline
    from .seed import seed_demo
    from .sim.scenarios import SCENARIOS, build_scenario
    from .sim.synth import render_truck
    cfg = Settings()
    cfg.data_dir = Path(tempfile.mkdtemp(prefix="gate-eval-"))
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    seed_demo(db, seed=a.seed)
    pipe = GatePipeline(db, cfg, SimulatedGateController(auto_close_s=0.1))
    rng = random.Random(a.seed)
    names = [s[0] for s in SCENARIOS]
    ok, total, per, ms = 0, 0, Counter(), []
    for i in range(a.n):
        name = names[i % len(names)]
        _, spec = build_scenario(db, rng, name)
        frames = render_truck(spec, rng.randint(0, 2**31))
        exp = dict((s[0], s[2]) for s in SCENARIOS)[name]
        ev = pipe.process(1 + i % 4, list(frames.values()), list(frames.keys()))
        good = ev["decision"] == exp
        ok += good
        total += 1
        per[(name, good)] += 1
        ms.append(ev["processing_ms"])
        mark = "ok " if good else "ERR"
        print(f"[{mark}] {name:18} expected {exp:13} got {ev['decision']:13} {ev['processing_ms']:5d} ms")
    print(f"\nDecision accuracy: {ok}/{total} = {100 * ok / total:.1f}%   "
          f"avg {sum(ms) / len(ms):.0f} ms / truck (4 cameras)")
    for n in names:
        print(f"  {n:18} {per[(n, True)]}/{per[(n, True)] + per[(n, False)]}")


def cmd_verify(a) -> None:
    """Acceptance check of all four feature areas on a fresh temporary database."""
    from .verify import main as verify_main
    verify_main(seed=a.seed)


def cmd_seed(a) -> None:
    from .seed import seed_demo
    db, _ = _pipeline()
    seed_demo(db, n_appointments=a.n, reset=a.reset)
    print(f"Seeded. {db.one('SELECT COUNT(*) AS n FROM appointments')['n']} appointments in {db.path}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="gate", description="Automated truck gate processing (computer vision)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run API + dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--simulate", action="store_true", help="start the multi-lane traffic simulator")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("process", help="process camera images of one truck")
    s.add_argument("images", nargs="+")
    s.add_argument("--lane", type=int, default=1)
    s.set_defaults(fn=cmd_process)

    s = sub.add_parser("generate", help="write synthetic truck images")
    s.add_argument("out_dir")
    s.add_argument("-n", type=int, default=10)
    s.add_argument("--scenario", default=None)
    s.add_argument("--seed", type=int, default=None)
    s.set_defaults(fn=cmd_generate)

    s = sub.add_parser("evaluate", help="measure end-to-end decision accuracy on simulated traffic")
    s.add_argument("-n", type=int, default=45)
    s.add_argument("--seed", type=int, default=42)
    s.set_defaults(fn=cmd_evaluate)

    s = sub.add_parser("verify", help="acceptance check of all features (gate, ETA, yard, driver messages)")
    s.add_argument("--seed", type=int, default=2026)
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("seed", help="load demo carriers / ASNs / appointments")
    s.add_argument("-n", type=int, default=40)
    s.add_argument("--reset", action="store_true")
    s.set_defaults(fn=cmd_seed)

    a = p.parse_args(argv)
    _setup_logging(a.verbose)
    if a.cmd in ("evaluate", "generate", "process"):
        logging.getLogger("gate.pipeline").setLevel(logging.WARNING)
    a.fn(a)


if __name__ == "__main__":
    main()
