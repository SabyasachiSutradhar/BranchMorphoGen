#!/usr/bin/env python3
"""
BranchMorphoGen Web GUI server
==============================

A thin, dependency-free (Python standard library only) web front end for the
BranchMorphoGen C++ simulator.  It never modifies or links against the core
code: it

  1. renders the parameters typed in the browser into a regular
     ``parameters.in`` file (same format the core parser expects),
  2. launches the unmodified ``BranchMorphoGen`` executable on that file inside
     a fresh run folder, and
  3. watches the files the simulator writes (Conf-*.swc, TemproralProps-*.csv,
     *.tif) and streams them to the browser as they appear (Server-Sent Events).

Usage
-----
    python3 webgui/server.py                 # opens http://127.0.0.1:8050
    python3 webgui/server.py --port 9000 --binary /path/to/BranchMorphoGen
    python3 webgui/server.py --build         # build the core with CMake first

"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import webbrowser
import zlib
from array import array
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # BranchMorphoGen repository root
STATIC = HERE / "static"
RUNS_DIR = HERE / "runs"

# --------------------------------------------------------------------------
# Parameter schema.  Order mirrors parameters.in so the generated file reads
# exactly like a hand-written one.  Kinds:
#   text, number, int, bool, choice, list (comma list), matrix ("a,b;c,d"),
#   ts  (time-dependent: rows of [time|None, value-string])
# --------------------------------------------------------------------------
SCHEMA = [
    {"group": "Simulation", "items": [
        ("SimulationName", "text", "Name prefix for every output file"),
        ("SpatialUnit", "text", "Spatial unit label (e.g. micron)"),
        ("TemporalUnit", "text", "Temporal unit label (e.g. min)"),
        ("NSample", "int", "Number of independent samples"),
        ("RunParallel", "bool", "Run samples in parallel threads"),
        ("Dimension", "choice:2|3", "Simulation dimension"),
        ("Dt", "number", "Time step (TemporalUnit)"),
        ("Time_Start", "number", "Simulation start time"),
        ("Time_End", "number", "Simulation end time"),
    ]},
    {"group": "Initial configuration", "items": [
        ("ReadFromSWC", "bool", "Start from an .swc file instead of a fresh soma"),
        ("SWCFileName", "text", "Input .swc file (upload one below, or a path)"),
        ("Root", "list", "Root (soma) position x,y,z"),
        ("N_BranchInitial", "text", "Number of primary branches, a range such as 2-4"),
        ("InitialLength", "number", "Length of initial branches"),
        ("InitialAngle", "number", "Angle of first primary branch (deg, <0 = random)"),
        ("InitialAngleWithZ", "number", "Angle of initial branches with Z (deg)"),
        ("RadiusSoma", "number", "Soma radius"),
        ("NascentBranchLength", "number", "Length of a newly spawned branch"),
        ("BranchInterval", "number", "Distance between points on a branch"),
        ("PersistanceLength", "number", "Persistence length (directional memory); inf allowed"),
    ]},
    {"group": "Boundary", "items": [
        ("LimitsX", "ts", "x-limits min,max (time-dependent)"),
        ("LimitsY", "ts", "y-limits min,max (time-dependent)"),
        ("LimitsZ", "ts", "z-limits min,max (time-dependent)"),
        ("Boundary_SoftLength", "number", "Width of the soft boundary"),
    ]},
    {"group": "Tip states", "items": [
        ("States", "list", "Tip states, e.g. 1,0,-1 (grow, pause, shrink)"),
        ("StateVelocity", "ts", "Velocity of each state (time-dependent)"),
        ("TransitionRates", "ts", "NState x NState rate matrix, rows separated by ';' (time-dependent)"),
    ]},
    {"group": "Branching", "items": [
        ("Bifurcation", "bool", "Tip bifurcation (true) or lateral side branching (false)"),
        ("BifurcationRatio", "number", "Bifurcation symmetry 0 (asym) - 1 (sym)"),
        ("BranchingAngleRef", "choice:local|global", "Reference frame for branching angle"),
        ("PolarAngleMode", "choice:fixed|uniform|reflective", "Polar angle mode (3D only)"),
        ("PolarBranchingAngle", "number", "Polar branching angle (deg)"),
        ("AzimuthalAngleMode", "choice:fixed|uniform|reflective", "Azimuthal angle mode (branching angle in 2D)"),
        ("AzimuthalBranchingAngle", "number", "Azimuthal branching angle (deg)"),
        ("BranchingAngleSTD", "number", "Std of branching-angle noise (deg)"),
        ("LengthDependentBranching", "bool", "Weight branching probability by length"),
        ("BranchingRate", "ts", "Branching rate per unit time (time-dependent)"),
        ("RebranchingProb", "number", "Probability an old branch branches again"),
        ("TerminationRate", "number", "Tip termination rate"),
    ]},
    {"group": "Collision", "items": [
        ("SelfAvoiding", "bool", "Tips self-avoid"),
        ("Interaction_Threshold", "number", "Collision distance"),
        ("FreezeCollidedBranch", "bool", "Collided branches stop branching"),
        ("Collision_States", "list", "States after collision"),
        ("Collision_StateVelocity", "list", "Velocities after collision"),
        ("PostCollisionPeriod", "number", "Duration of post-collision dynamics; inf allowed"),
        ("Collision_TransitionRates", "matrix", "Post-collision rate matrix, rows separated by ';'"),
    ]},
    {"group": "Forces", "items": [
        ("StraightenBranches", "bool", "Relax branches with mechanical forces"),
        ("E_Axial", "number", "Axial modulus"),
        ("E_Bending", "number", "Bending modulus"),
        ("DragCoef", "number", "Drag coefficient"),
    ]},
    {"group": "Radius", "items": [
        ("MinimumRadius", "number", "Minimum branch radius"),
        ("MaximumRadius", "number", "Maximum branch radius"),
        ("RadiusModel", "choice:None|TipNumber|BranchNumber|Length|Area|Volume", "Radius model"),
        ("BetaRadius", "number", "Radius increment constant"),
    ]},
    {"group": "Output", "items": [
        ("N_SWC", "int", "Number of snapshots written (= frames streamed to the viewer)"),
        ("MAX_IMAGE_SIZE", "number", "Image field of view (-1 = use limits)"),
        ("ImagePlane", "choice:xy|xz|yz", "Image projection plane"),
        ("pixelsize", "number", "TIFF pixel size"),
        ("SignalMean", "number", "Signal mean"),
        ("SignalStd", "number", "Signal std"),
        ("BackgroundMean", "number", "Background mean"),
        ("BackgroundStd", "number", "Background std"),
    ]},
]
KINDS = {k: kind for g in SCHEMA for (k, kind, _h) in g["items"]}
ORDER = [k for g in SCHEMA for (k, _kind, _h) in g["items"]]


def schema_json():
    out = []
    for g in SCHEMA:
        out.append({"group": g["group"], "items": [
            {"key": k, "kind": kind, "help": h} for (k, kind, h) in g["items"]]})
    return out


# --------------------------------------------------------------------------
# parameters.in <-> dict   (mirrors the logic of SimulationParameters.cpp)
# --------------------------------------------------------------------------
_COMMENT = re.compile(r"(//|#|%)")


def _strip(line: str) -> str:
    m = _COMMENT.search(line)
    return (line[: m.start()] if m else line).strip()


def _clean_val(v: str) -> str:
    v = v.strip()
    if v.endswith(";"):
        v = v[:-1].strip()
    return v


def parse_param_text(text: str) -> dict:
    """Parse a parameters.in text into {key: value}.  Time-dependent keys get
    a list of [time or None, value-string] rows."""
    vals: dict = {}
    lines = [_strip(l) for l in text.splitlines()]
    lines = [l for l in lines if l]
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), _clean_val(val)
        if KINDS.get(key) == "ts":
            block = [val] if val else []
            while i < len(lines) and "=" not in lines[i]:
                block.append(_clean_val(lines[i]))
                i += 1
            rows = []
            for b in block:
                if not b:
                    continue
                if ":" in b:
                    t, v = b.split(":", 1)
                    rows.append([t.strip(), _clean_val(v)])
                else:
                    rows.append([None, b])
                    break
            vals[key] = rows
        else:
            vals[key] = val
    return vals


def render_param_text(vals: dict) -> str:
    out = [
        "################## Generated by the BranchMorphoGen Web GUI",
        f"################## {datetime.now():%Y-%m-%d %H:%M:%S}",
        "",
    ]
    for g in SCHEMA:
        out.append(f"########## {g['group']}")
        for (k, kind, h) in g["items"]:
            if k not in vals or vals[k] is None:
                continue
            v = vals[k]
            if kind == "ts":
                rows = [r for r in v if str(r[1]).strip() != ""]
                if not rows:
                    continue
                if len(rows) == 1 and (rows[0][0] in (None, "")):
                    out.append(f"    {k}={_clean_val(str(rows[0][1]))};")
                else:
                    out.append(f"    {k}=")
                    for t, val in rows:
                        out.append(f"    {str(t).strip()}:{_clean_val(str(val))};")
            elif kind == "bool":
                b = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes")
                out.append(f"    {k}={'true' if b else 'false'};")
            else:
                sval = _clean_val(str(v))
                if re.search(r"(//|#|%)", sval):
                    raise ValueError(f"{k}: value may not contain //, # or %")
                out.append(f"    {k}={sval};")
        out.append("")
    out.append("######## END OF INPUT PARAMETERS")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# SWC handling
# --------------------------------------------------------------------------
def read_swc_decimated(path: Path, res: float):
    """Read an SWC written by the simulator and thin points along unbranched
    stretches so the browser receives a few thousand nodes instead of tens of
    thousands.  Branch points, tips and the root are always kept."""
    ids, xs, ys, zs, rs, ps = [], [], [], [], [], []
    with open(path, "r") as f:
        for line in f:
            if not line or line[0] == "#":
                continue
            parts = line.split()
            if len(parts) < 7:
                continue
            ids.append(int(parts[0]))
            xs.append(float(parts[2])); ys.append(float(parts[3])); zs.append(float(parts[4]))
            rs.append(float(parts[5])); ps.append(int(parts[6]))
    n = len(ids)
    index = {sid: i for i, sid in enumerate(ids)}
    par = [index.get(p, -1) for p in ps]
    nchild = [0] * n
    for p in par:
        if p >= 0:
            nchild[p] += 1

    total_len = 0.0
    seglen = [0.0] * n
    for i in range(n):
        p = par[i]
        if p >= 0:
            d = math.sqrt((xs[i] - xs[p]) ** 2 + (ys[i] - ys[p]) ** 2 + (zs[i] - zs[p]) ** 2)
            seglen[i] = d
            total_len += d

    # single pass works because the simulator writes parents before children;
    # fall back to keeping everything otherwise
    ordered = all(par[i] < i for i in range(n))
    keep_anc = [-1] * n
    acc = [0.0] * n
    new_index = [-1] * n
    ox, oy, oz, orr, op = [], [], [], [], []
    for i in range(n):
        p = par[i]
        if not ordered or p < 0:
            keep = True
        else:
            acc[i] = acc[p] + seglen[i]
            keep = nchild[i] != 1 or nchild[p] != 1 or acc[i] >= res
        if keep:
            acc[i] = 0.0
            new_index[i] = len(ox)
            ox.append(round(xs[i], 2)); oy.append(round(ys[i], 2)); oz.append(round(zs[i], 2))
            orr.append(round(rs[i], 3))
            if not ordered:
                op.append(p)
            else:
                anc = keep_anc[p] if p >= 0 else -1
                op.append(new_index[anc] if anc >= 0 else -1)
            keep_anc[i] = i
        else:
            keep_anc[i] = keep_anc[p]
    tips = sum(1 for c in nchild if c == 0)
    bpoints = sum(1 for c in nchild if c >= 2)
    return {"x": ox, "y": oy, "z": oz, "r": orr, "p": op,
            "nPoints": n, "tips": tips, "branchPoints": bpoints,
            "totalLength": round(total_len, 3)}


# --------------------------------------------------------------------------
# Minimal uncompressed TIFF (16-bit gray) -> PNG preview, stdlib only
# --------------------------------------------------------------------------
def tiff_to_png(path: Path, max_side: int = 1024) -> bytes:
    data = path.read_bytes()
    bo = "<" if data[:2] == b"II" else ">"
    ifd = struct.unpack(bo + "I", data[4:8])[0]
    ntag = struct.unpack(bo + "H", data[ifd:ifd + 2])[0]
    tags = {}
    tsize = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8, 11: 4, 12: 8}
    for k in range(ntag):
        e = ifd + 2 + 12 * k
        tag, typ, cnt = struct.unpack(bo + "HHI", data[e:e + 8])
        sz = tsize.get(typ, 1) * cnt
        fmt = {3: "H", 4: "I"}.get(typ)
        if fmt is None:
            continue
        if sz <= 4:
            raw = data[e + 8:e + 8 + sz]
        else:
            off = struct.unpack(bo + "I", data[e + 8:e + 12])[0]
            raw = data[off:off + sz]
        tags[tag] = list(struct.unpack(bo + fmt * cnt, raw))
    w, h = tags[256][0], tags[257][0]
    bps = tags.get(258, [16])[0]
    if tags.get(259, [1])[0] != 1:
        raise ValueError("compressed TIFF not supported")
    offs, counts = tags[273], tags[279]
    buf = b"".join(data[o:o + c] for o, c in zip(offs, counts))
    pix = array("H" if bps == 16 else "B")
    pix.frombytes(buf[: w * h * (bps // 8)])
    if bps == 16 and (bo == ">") != (sys.byteorder == "big"):
        pix.byteswap()
    stride = max(1, math.ceil(max(w, h) / max_side))
    rows = [pix[y * w:(y + 1) * w:stride] for y in range(0, h, stride)]
    ow, oh = len(rows[0]), len(rows)
    sample = sorted(pix[:: max(1, len(pix) // 20000)])
    lo = sample[int(0.01 * (len(sample) - 1))]
    hi = sample[int(0.9995 * (len(sample) - 1))]
    if hi <= lo:
        hi = lo + 1
    scale = 255.0 / (hi - lo)
    lut_cache = {}
    raw = bytearray()
    for r in rows:
        raw.append(0)
        for v in r:
            c = lut_cache.get(v)
            if c is None:
                c = int(max(0, min(255, (v - lo) * scale)))
                lut_cache[v] = c
            raw.append(c)

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", ow, oh, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------
# Run manager
# --------------------------------------------------------------------------
class Run:
    def __init__(self, run_id: str, folder: Path, sim_name: str, proc: subprocess.Popen | None):
        self.id = run_id
        self.dir = folder
        self.sim_name = sim_name
        self.proc = proc
        self.started = time.time()
        self.ended: float | None = None
        self.returncode: int | None = None
        self.stopped = False

    def poll(self):
        if self.proc is not None and self.returncode is None:
            rc = self.proc.poll()
            if rc is not None:
                self.returncode = rc
                self.ended = time.time()
        return self.returncode

    @property
    def running(self):
        return self.proc is not None and self.poll() is None


RUNS: dict[str, Run] = {}
RUNS_LOCK = threading.Lock()
BINARY: Path | None = None


def find_binary(explicit: str | None) -> Path | None:
    cands = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    for d in ("build", "bin", "."):
        cands += [ROOT / d / "BranchMorphoGen", ROOT / d / "BranchMorphoGen.exe"]
    w = shutil.which("BranchMorphoGen")
    if w:
        cands.append(Path(w))
    for c in cands:
        if c.is_file() and os.access(c, os.X_OK):
            return c.resolve()
    return None


def build_core() -> None:
    bdir = ROOT / "build"
    bdir.mkdir(exist_ok=True)
    print("[webgui] building BranchMorphoGen with CMake in", bdir)
    subprocess.check_call(["cmake", ".."], cwd=bdir)
    subprocess.check_call(["cmake", "--build", ".", "-j"], cwd=bdir)


def safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", s.strip()) or "sim"
    return s[:60]


def stop_run(run: Run):
    if run.running:
        run.stopped = True
        try:
            run.proc.terminate()
            try:
                run.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                run.proc.kill()
        except Exception:
            pass
        run.poll()


def start_run(values: dict, swc_text: str | None, overrides: dict | None, keep_previous: bool) -> Run:
    if BINARY is None:
        raise RuntimeError("BranchMorphoGen executable not found. Build the core (see README) "
                           "or start the server with --binary /path/to/BranchMorphoGen or --build.")
    if not keep_previous:
        with RUNS_LOCK:
            for r in RUNS.values():
                stop_run(r)
    vals = dict(values)
    for k, v in (overrides or {}).items():
        if k in KINDS:
            vals[k] = v
    vals["SimulationName"] = safe_name(str(vals.get("SimulationName", "sim")))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"{stamp}-{vals['SimulationName']}"
    n = 1
    while (RUNS_DIR / run_id).exists():
        n += 1
        run_id = f"{stamp}-{vals['SimulationName']}-{n}"
    folder = RUNS_DIR / run_id
    folder.mkdir(parents=True)
    if str(vals.get("ReadFromSWC", "false")).lower() in ("true", "1", "yes"):
        if swc_text:
            (folder / "INPUT.swc").write_text(swc_text)
            vals["SWCFileName"] = "INPUT.swc"
        else:
            p = Path(str(vals.get("SWCFileName", ""))).expanduser()
            if not p.is_absolute():
                p = ROOT / p
            if p.exists():
                vals["SWCFileName"] = str(p.resolve())
            else:
                raise RuntimeError(f"ReadFromSWC is true but the SWC file '{vals.get('SWCFileName')}' "
                                   "was not found. Upload one in the panel.")
    text = render_param_text(vals)
    (folder / "parameters.in").write_text(text)
    (folder / "gui_values.json").write_text(json.dumps(
        {"values": values, "overrides": overrides or {}}, indent=1))
    log = open(folder / "run.log", "w")
    kw = {}
    if os.name == "posix":
        kw["start_new_session"] = True
    proc = subprocess.Popen([str(BINARY), "parameters.in"], cwd=folder,
                            stdout=log, stderr=subprocess.STDOUT, **kw)
    run = Run(run_id, folder, vals["SimulationName"], proc)
    with RUNS_LOCK:
        RUNS[run_id] = run
    return run


def get_run(run_id: str) -> Run | None:
    with RUNS_LOCK:
        r = RUNS.get(run_id)
    if r:
        return r
    folder = RUNS_DIR / run_id
    if folder.is_dir() and (folder / "parameters.in").exists():
        vals = parse_param_text((folder / "parameters.in").read_text())
        r = Run(run_id, folder, str(vals.get("SimulationName", "sim")), None)
        with RUNS_LOCK:
            RUNS[run_id] = r
        return r
    return None


def list_runs():
    out = []
    if RUNS_DIR.is_dir():
        for d in sorted(RUNS_DIR.iterdir(), reverse=True):
            if d.is_dir() and (d / "parameters.in").exists():
                r = RUNS.get(d.name)
                out.append({"id": d.name, "running": bool(r and r.running),
                            "frames": len(list(d.glob("*-Conf-*.swc")))})
    return out[:100]


FRAME_RE = re.compile(r"-Conf-(\d+)-time-([-+0-9.eE]+)\.swc$")


def scan_frames(run: Run):
    """Return list of (sample, time, swc_path, complete) for frames on disk."""
    frames = []
    prefix = run.sim_name + "-Conf-"
    finished = not run.running
    for p in run.dir.glob(f"{run.sim_name}-Conf-*.swc"):
        if not p.name.startswith(prefix):
            continue
        m = FRAME_RE.search(p.name)
        if not m:
            continue
        s, t = int(m.group(1)), m.group(2)
        # The simulator writes SWC -> TIFF -> BranchStats; once the stats file
        # exists the SWC is complete.
        stats = run.dir / f"{run.sim_name}-BranchStats-{s}-time-{t}.csv"
        frames.append((s, float(t), t, p, finished or stats.exists()))
    frames.sort(key=lambda f: (f[0], f[1]))
    return frames


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript", ".css": "text/css",
        ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json",
        ".swc": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
        ".in": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
        ".tif": "image/tiff"}


class Handler(BaseHTTPRequestHandler):
    server_version = "BranchMorphoGenGUI/1.0"

    def log_message(self, fmt, *args):  # quieter console
        if "/api/events" in (args[0] if args else ""):
            return
        sys.stderr.write("[webgui] " + (fmt % args) + "\n")

    # ---- helpers
    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    # ---- routes
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/logo.svg" and (ROOT / "docs" / "logo.svg").exists():
                return self._send(200, (ROOT / "docs" / "logo.svg").read_bytes(), MIME[".svg"])
            if path == "/api/schema":
                return self._schema()
            if path == "/api/runs":
                return self._json({"runs": list_runs()})
            if path == "/api/events":
                return self._events(q)
            if path == "/api/image":
                return self._image(q)
            if path.startswith("/files/"):
                return self._file(path)
            self._send(404, b"not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as e:  # pragma: no cover
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = self._body()
            if u.path == "/api/run":
                run = start_run(body.get("values", {}), body.get("swc_text"),
                                body.get("overrides"), bool(body.get("keep_previous")))
                return self._json({"run": run.id})
            if u.path == "/api/stop":
                r = get_run(body.get("run", ""))
                if r:
                    stop_run(r)
                else:
                    with RUNS_LOCK:
                        for r in RUNS.values():
                            stop_run(r)
                return self._json({"ok": True})
            if u.path == "/api/parse":
                return self._json({"values": parse_param_text(body.get("text", ""))})
            if u.path == "/api/render":
                return self._send(200, render_param_text(body.get("values", {})).encode(), MIME[".in"])
            self._send(404, b"not found", "text/plain")
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def _static(self, rel):
        p = (STATIC / rel).resolve()
        if STATIC not in p.parents and p != STATIC or not p.is_file():
            return self._send(404, b"not found", "text/plain")
        self._send(200, p.read_bytes(), MIME.get(p.suffix, "application/octet-stream"))

    def _schema(self):
        template = ROOT / "parameters.in"
        defaults = parse_param_text(template.read_text()) if template.exists() else {}
        presets = {}
        for f in sorted(ROOT.glob("*.in")):
            presets[f.name] = parse_param_text(f.read_text())
        self._json({"schema": schema_json(), "defaults": defaults, "presets": presets,
                    "binary": str(BINARY) if BINARY else None, "root": str(ROOT)})

    def _file(self, path):
        parts = unquote(path).split("/")
        if len(parts) != 4:
            return self._send(404, b"not found", "text/plain")
        run = get_run(parts[2])
        if not run:
            return self._send(404, b"no such run", "text/plain")
        p = (run.dir / parts[3]).resolve()
        if p.parent != run.dir.resolve() or not p.is_file():
            return self._send(404, b"not found", "text/plain")
        self._send(200, p.read_bytes(), MIME.get(p.suffix, "application/octet-stream"),
                   {"Content-Disposition": f'attachment; filename="{p.name}"'})

    def _image(self, q):
        run = get_run(q.get("run", ""))
        if not run:
            return self._send(404, b"no such run", "text/plain")
        p = (run.dir / q.get("file", "")).resolve()
        if p.parent != run.dir.resolve() or p.suffix != ".tif" or not p.is_file():
            return self._send(404, b"not found", "text/plain")
        self._send(200, tiff_to_png(p, int(q.get("max", 1024))), "image/png")

    def _events(self, q):
        run = get_run(q.get("run", ""))
        if not run:
            return self._send(404, b"no such run", "text/plain")
        res = max(0.0, float(q.get("res", 0.5)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def emit(event, data):
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        emit("start", {"run": run.id, "simName": run.sim_name,
                       "params": parse_param_text((run.dir / "parameters.in").read_text())})
        sent = set()
        stats_pos: dict[int, int] = {}
        log_pos = 0
        last_beat = time.time()
        while True:
            running = run.running
            # log
            logf = run.dir / "run.log"
            if logf.exists():
                with open(logf, "r", errors="replace") as f:
                    f.seek(log_pos)
                    chunk = f.read()
                    log_pos = f.tell()
                if chunk:
                    emit("log", {"text": chunk})
            # temporal stats (one row per time unit)
            for sf in run.dir.glob(f"{run.sim_name}-TemproralProps-*.csv"):
                m = re.search(r"-TemproralProps-(\d+)\.csv$", sf.name)
                if not m:
                    continue
                s = int(m.group(1))
                with open(sf, "r") as f:
                    f.seek(stats_pos.get(s, 0))
                    chunk = f.read()
                    # only consume complete lines
                    cut = chunk.rfind("\n") + 1
                    stats_pos[s] = stats_pos.get(s, 0) + len(chunk[:cut].encode())
                rows = []
                for line in chunk[:cut].splitlines():
                    if not line or line.startswith("#"):
                        continue
                    try:
                        rows.append([float(x) for x in line.split(",")[:5]])
                    except ValueError:
                        continue
                if rows:
                    emit("stats", {"sample": s, "rows": rows})
            # frames
            for s, t, tstr, p, complete in scan_frames(run):
                key = (s, tstr)
                if key in sent or not complete:
                    continue
                try:
                    fr = read_swc_decimated(p, res)
                except Exception as e:
                    emit("log", {"text": f"[webgui] could not read {p.name}: {e}\n"})
                    sent.add(key)
                    continue
                sent.add(key)
                tif = p.with_suffix(".tif")
                fr.update({"sample": s, "t": t, "swc": p.name,
                           "tif": tif.name if tif.exists() else None})
                emit("frame", fr)
            if not running:
                emit("done", {"returncode": run.returncode, "stopped": run.stopped,
                              "elapsed": round((run.ended or time.time()) - run.started, 1)
                              if run.proc else None})
                return
            if time.time() - last_beat > 10:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                last_beat = time.time()
            time.sleep(0.4)


def main():
    global BINARY
    ap = argparse.ArgumentParser(description="BranchMorphoGen web GUI")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--binary", help="path to the BranchMorphoGen executable")
    ap.add_argument("--build", action="store_true", help="build the core with CMake if needed")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    BINARY = find_binary(a.binary)
    if BINARY is None and a.build:
        build_core()
        BINARY = find_binary(a.binary)
    RUNS_DIR.mkdir(exist_ok=True)
    print("=" * 60)
    print(" BranchMorphoGen Web GUI")
    print(f" core executable : {BINARY or 'NOT FOUND (build it, or use --binary / --build)'}")
    print(f" run folders     : {RUNS_DIR}")
    url = f"http://{a.host}:{a.port}/"
    print(f" open            : {url}")
    print("=" * 60)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True

    def shutdown(*_):
        with RUNS_LOCK:
            for r in RUNS.values():
                stop_run(r)
        os._exit(0)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
