#!/usr/bin/env python3
"""Read-only Riello watch: LAN page, minute log, Telegram on transition.

No instant commands. The UPS load counter stays at 0, so watts are a fixed estimate.
"""
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BIND = os.environ.get("UPS_WATCH_BIND", "127.0.0.1")
PORT = int(os.environ.get("UPS_WATCH_PORT", "8420"))
UPS = os.environ.get("UPS_WATCH_UPS", "ups@localhost")
LOG = Path(os.environ.get("UPS_WATCH_LOG", "/var/log/ups-watch/ups.csv"))
STATE = Path(os.environ.get("UPS_WATCH_STATE", "/var/lib/ups-watch/state.json"))
FSD_SEC = int(os.environ.get("UPS_WATCH_FSD_SEC", "3600"))
WATTS = float(os.environ.get("UPS_WATCH_WATTS", "55"))
# shortcut: load counter is dead, upgrade when output.L1.realpower is non-zero
# Resting-ish map for this 24V pack. 25.2V was called ~85% on 2026-11-14.
# Charging voltage reads high. Not a coulomb counter.
POINTS = ((21.0, 0), (23.5, 20), (24.2, 50), (24.8, 70), (25.2, 85), (25.8, 100))

snap = {
    "ok": False,
    "error": "start",
    "ts": None,
    "status": "?",
    "vbat": None,
    "vin": None,
    "soc": None,
    "minutes": None,
    "fsd": "idle",
    "watts": WATTS,
    "note": "",
}
lock = threading.Lock()


def soc_est(vbat: float) -> int:
    if vbat <= POINTS[0][0]:
        return POINTS[0][1]
    if vbat >= POINTS[-1][0]:
        return POINTS[-1][1]
    for (v0, p0), (v1, p1) in zip(POINTS, POINTS[1:]):
        if v0 <= vbat <= v1:
            return round(p0 + (p1 - p0) * (vbat - v0) / (v1 - v0))
    return POINTS[-1][1]


def minutes_est(soc: int, watts: float | None = None) -> int:
    # 2x12V 9Ah, inverter ~70%, Peukert 1.25 against the 20h rate.
    amps = ((WATTS if watts is None else watts) / 0.70) / 24.0
    hours = 20.0 * (0.45 / amps) ** 1.25 * (soc / 100.0)
    return max(0, round(hours * 60))


def watts_of(data: dict) -> tuple[float, bool]:
    for key in ("output.L1.realpower", "ups.realpower"):
        try:
            val = float(data.get(key, ""))
        except (TypeError, ValueError):
            continue
        if val > 0:
            return val, False
    return WATTS, True


def parse_upsc(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        out[key.strip()] = val.strip()
    return out


def read_ups() -> dict:
    proc = subprocess.run(
        ["upsc", UPS],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "upsc failed").strip().splitlines()
        raise RuntimeError(err[-1] if err else "upsc failed")
    data = parse_upsc(proc.stdout)
    if "ups.status" not in data:
        raise RuntimeError("ups.status missing")
    return data


def fsd_label(flags: set, onbatt_since: float | None, now: float) -> str:
    if "LB" in flags:
        return "now"
    if "OB" not in flags or not onbatt_since:
        return "idle"
    left = max(0, FSD_SEC - int(now - onbatt_since))
    return f"in {left // 60} min {left % 60:02d} s"


def note_for(flags: set, dead: bool) -> str:
    bits = []
    if dead:
        bits.append(f"UPS watt meter is dead, assumed {WATTS:.0f} W")
    if "CHRG" in flags:
        bits.append("charging, voltage SoC reads high")
    return ". ".join(bits)


def load_state() -> dict:
    if not STATE.exists():
        return {"flags": [], "onbatt_since": None, "seeded": False}
    return json.loads(STATE.read_text())


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    thread = os.environ.get("TELEGRAM_THREAD_ID", "")
    if not token or not chat:
        print("telegram skipped: no token", flush=True)
        return
    body = urllib.parse.urlencode(
        {
            "chat_id": chat,
            "message_thread_id": thread,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
        if not payload.get("ok"):
            print("telegram not ok", payload.get("error_code"), flush=True)
    except Exception as exc:
        print("telegram failed", type(exc).__name__, flush=True)


def alert_for(prev: set, flags: set) -> str | None:
    if "LB" in flags and "LB" not in prev:
        return "UPS: LB. Shutdown now."
    if "OB" in flags and "OB" not in prev:
        return f"UPS: mains lost. On battery. FSD in {FSD_SEC // 60} min, or sooner on LB."
    if "OB" not in flags and "OB" in prev:
        return "UPS: mains back."
    return None


def append_log(ts: str, status: str, vbat, vin, soc) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 2_000_000:
        old = LOG.with_suffix(".csv.1")
        old.unlink(missing_ok=True)
        LOG.replace(old)
    line = f"{ts},{status},{vbat},{vin},{soc}\n"
    with LOG.open("a") as fh:
        fh.write(line)


def publish(data: dict, state: dict, now: float) -> None:
    flags = set(data["ups.status"].split())
    vbat = float(data["battery.voltage"])
    vin = float(data["input.voltage"])
    soc = soc_est(vbat)
    watts, dead = watts_of(data)
    ts = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
    row = {
        "ok": True,
        "error": "",
        "ts": ts,
        "status": data["ups.status"],
        "model": data.get("device.model") or data.get("ups.model") or "",
        "vbat": vbat,
        "vin": vin,
        "soc": soc,
        "minutes": minutes_est(soc, watts),
        "fsd": fsd_label(flags, state.get("onbatt_since"), now),
        "watts": watts,
        "meter_dead": dead,
        "note": note_for(flags, dead),
    }
    with lock:
        snap.update(row)


PAGE = """
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UPS · Riello</title>
<style>
  :root {
    --bg: #0c0b09;
    --panel: #17140f;
    --cream: #f4e7c8;
    --ink: #1c1814;
    --brass: #c6a56a;
    --muted: #9a8b74;
    --green: #3ddc84;
    --amber: #f0b429;
    --red: #e23b32;
    --dim: #3a342c;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--cream); }
  body {
    min-height: 100vh;
    font: 15px/1.35 "Liberation Sans", "DejaVu Sans", "Nimbus Sans", sans-serif;
  }
  .panel {
    max-width: 1040px;
    margin: 16px auto;
    padding: 26px 22px 22px;
    position: relative;
    background:
      radial-gradient(900px 280px at 50% 0%, #2c261c, transparent 62%),
      var(--panel);
    border: 1px solid #3d3428;
    border-radius: 18px;
    box-shadow: 0 22px 50px rgba(0,0,0,.45), inset 0 1px 0 rgba(240,226,196,.14);
  }
  .screw {
    position: absolute; width: 12px; height: 12px; border-radius: 50%;
    background: radial-gradient(circle at 35% 35%, #f0e2c4, #8a7044 42%, #2c261c 74%);
    box-shadow: inset 0 0 0 1px #1a1612, 0 1px 0 rgba(240,226,196,.25);
  }
  .screw:before {
    content: ""; position: absolute; left: 2px; right: 2px; top: 5px; height: 1.5px;
    background: #1a1612; transform: rotate(32deg);
  }
  .screw.a { top: 10px; left: 10px; }
  .screw.b { top: 10px; right: 10px; }
  .screw.c { bottom: 10px; left: 10px; }
  .screw.d { bottom: 10px; right: 10px; }
  header { display: flex; justify-content: space-between; gap: 16px; align-items: flex-end; padding: 4px 8px 12px; }
  h1 { margin: 0; font-size: 18px; font-weight: 650; letter-spacing: .18em; color: #f0e2c4; }
  .sub { margin: 4px 0 0; color: var(--muted); font-size: 12px; letter-spacing: .06em; }
  .lamps { display: flex; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }
  .lamp { width: 58px; text-align: center; }
  .jewel {
    display: block;
    width: 18px; height: 18px; margin: 0 auto 4px; border-radius: 50%;
    background: radial-gradient(circle at 35% 30%, #8a7d68, #1a1612 70%);
    box-shadow: inset 0 0 0 2px #c6a56a, 0 0 0 1px #3a3024;
  }
  .jewel.on-g { background: radial-gradient(circle at 35% 30%, #d8ffe8, var(--green) 46%, #146b3a); box-shadow: inset 0 0 0 2px #8a7044, 0 0 10px var(--green); }
  .jewel.on-a { background: radial-gradient(circle at 35% 30%, #fff6d0, #ffbf1a 42%, #a35e00); box-shadow: inset 0 0 0 2px #c6a56a, 0 0 12px #ffbf1a; }
  .jewel.on-r { background: radial-gradient(circle at 35% 30%, #ffd0c8, var(--red) 46%, #7a1c16); box-shadow: inset 0 0 0 2px #8a7044, 0 0 10px var(--red); }
  .jewel.blink { animation: blink 1s steps(2) infinite; }
  .lamp span { display: block; font-size: 9px; letter-spacing: .12em; color: var(--muted); }
  @keyframes blink { 50% { opacity: .25; } }
  @media (prefers-reduced-motion: reduce) { .jewel.blink { animation: none; } }
  .meters { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 4px; }
  .meter { text-align: center; min-width: 0; }
  .meter svg { width: min(100%, 210px); height: auto; display: block; margin: 0 auto; }
  .dock { display: grid; grid-template-columns: 168px 1fr; gap: 14px; align-items: center; margin: 6px 8px 0; }
  .dock .meter svg { width: 150px; }
  @media (max-width: 760px) {
    .meters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .dock { grid-template-columns: 1fr; }
  }
  .needle { transform-box: view-box; transform-origin: center; transform: rotate(-120deg); transition: transform .65s cubic-bezier(.22,.8,.28,1); }
  @media (prefers-reduced-motion: reduce) { .needle { transition: none; } }
  .cap { margin-top: -6px; font-size: 11px; letter-spacing: .16em; color: var(--brass); }
  .win {
    margin: 6px auto 0; width: 112px; padding: 4px 0 3px;
    background: #12100c; color: #f4e7c8; border-radius: 4px;
    box-shadow: inset 0 0 0 1px #3d3428, inset 0 8px 12px rgba(0,0,0,.35);
    font-variant-numeric: tabular-nums; font-size: 18px; letter-spacing: .04em;
  }
  .win small { display: block; color: #d2c4a8; font-size: 10px; letter-spacing: .08em; }
  .plaque {
    margin: 0; padding: 10px 12px;
    background: #221c14; color: #e4d3ae; border-radius: 8px;
    box-shadow: inset 0 0 0 1px #4a3c28;
    font-size: 13px;
  }
  .plaque p { margin: 0 0 4px; }
  .fine { color: var(--muted); font-size: 12px; }
  .bad { color: #ffb0a4; }
  .counter {
    margin-left: 8px; font-variant-numeric: tabular-nums; letter-spacing: .08em;
    color: #f4e7c8;
  }
</style>
<div class="panel" id="panel">
  <i class="screw a"></i><i class="screw b"></i><i class="screw c"></i><i class="screw d"></i>
  <header>
    <div>
      <h1>RIELLO iDIALOG 1600</h1>
      <p class="sub">IDG 1600 · 24 V · 2×9 Ah · read only · <span class="counter" id="clock">—</span></p>
    </div>
    <div class="lamps" id="lamps"></div>
  </header>
  <div class="meters" id="meters"></div>
  <div class="dock">
    <div id="dock-meter"></div>
    <div class="plaque" aria-live="polite">
      <p id="note">reading…</p>
      <p id="extra"></p>
      <p class="fine">The countdown on this page is not the NUT timer. The real one is upssched: one hour, or immediately on LB.</p>
    </div>
  </div>
</div>
<noscript><p class="bad">This page needs JS. JSON is at /json.</p></noscript>
<script>
const NS = 'http://www.w3.org/2000/svg';
const SPECS = [
  { id: 'vin', label: 'MAINS', title: 'MAINS', unit: 'V', min: 200, max: 270, major: 10, every: 20, minor: 5, digits: 0,
    zones: [[200, 216, '#a33b32'], [216, 253, '#2f7d4a'], [253, 260, '#c4882a'], [260, 270, '#a33b32']] },
  { id: 'vbat', label: 'BATT', title: 'BATT', unit: 'V', min: 20, max: 28, major: 2, every: 2, minor: 1, digits: 1,
    zones: [[20, 22.5, '#a33b32'], [22.5, 24.2, '#c4882a'], [24.2, 26.4, '#2f7d4a'], [26.4, 28, '#c4882a']] },
  { id: 'soc', label: 'CHARGE', title: 'CHARGE', unit: '%', min: 0, max: 100, major: 20, every: 20, minor: 10, digits: 0, est: true,
    zones: [[0, 20, '#a33b32'], [20, 40, '#c4882a'], [40, 100, '#2f7d4a']] },
  { id: 'min', label: 'RUNTIME', title: 'RUNTIME', unit: 'min', min: 0, max: 120, major: 30, every: 30, minor: 10, digits: 0, est: true,
    zones: [[0, 30, '#a33b32'], [30, 60, '#c4882a'], [60, 120, '#2f7d4a']] },
  { id: 'w', label: 'WATTS', title: 'WATTS', unit: 'W', min: 0, max: 100, major: 25, every: 25, minor: 5, digits: 0, est: true, dead: true, dock: true,
    zones: [[45, 75, '#c4882a']] }
];
const LAMPS = [
  ['ol', 'MAINS'], ['bat', 'BAT'], ['chrg', 'CHRG'], ['lb', 'LB'], ['fsd', 'FSD'], ['link', 'LINK']
];
const meters = {};
let last = null;

function svgEl(name, attrs) {
  const n = document.createElementNS(NS, name);
  Object.keys(attrs).forEach(function (k) { n.setAttribute(k, attrs[k]); });
  return n;
}
function deg(v, spec) {
  const t = Math.max(0, Math.min(1, (v - spec.min) / (spec.max - spec.min)));
  return -120 + t * 240;
}
function polar(r, angle) {
  const a = (angle - 90) * Math.PI / 180;
  return [100 + r * Math.cos(a), 100 + r * Math.sin(a)];
}
function arc(r, a0, a1) {
  const p0 = polar(r, a0);
  const p1 = polar(r, a1);
  const large = (a1 - a0) > 180 ? 1 : 0;
  return 'M ' + p0[0].toFixed(1) + ' ' + p0[1].toFixed(1) + ' A ' + r + ' ' + r + ' 0 ' + large + ' 1 ' + p1[0].toFixed(1) + ' ' + p1[1].toFixed(1);
}
function build() {
  const lamps = document.getElementById('lamps');
  LAMPS.forEach(function (pair) {
    const d = document.createElement('div');
    d.className = 'lamp';
    d.innerHTML = '<i class="jewel" id="lamp-' + pair[0] + '"></i><span>' + pair[1] + '</span>';
    lamps.appendChild(d);
  });
  SPECS.forEach(function (spec) {
    const root = document.getElementById(spec.dock ? 'dock-meter' : 'meters');
    const card = document.createElement('div');
    card.className = 'meter';
    const svg = svgEl('svg', { viewBox: '0 0 200 200', 'aria-hidden': 'true' });
    svg.appendChild(svgEl('circle', { cx: 100, cy: 100, r: 96, fill: '#c6a56a' }));
    svg.appendChild(svgEl('circle', { cx: 100, cy: 100, r: 90, fill: '#1a1612' }));
    svg.appendChild(svgEl('circle', { cx: 100, cy: 100, r: 84, fill: '#f3e6c8' }));
    spec.zones.forEach(function (z) {
      svg.appendChild(svgEl('path', {
        d: arc(76, deg(z[0], spec), deg(z[1], spec)),
        fill: 'none', stroke: z[2], 'stroke-width': 8, opacity: 0.55
      }));
    });
    const steps = Math.round((spec.max - spec.min) / spec.minor);
    for (let i = 0; i <= steps; i++) {
      const v = spec.min + i * spec.minor;
      const a = deg(v, spec);
      const major = Math.abs(v / spec.major - Math.round(v / spec.major)) < 0.01;
      const p1 = polar(major ? 64 : 70, a);
      const p2 = polar(78, a);
      svg.appendChild(svgEl('line', {
        x1: p1[0].toFixed(1), y1: p1[1].toFixed(1), x2: p2[0].toFixed(1), y2: p2[1].toFixed(1),
        stroke: '#1c1814', 'stroke-width': major ? 2 : 1
      }));
      const show = major && Math.abs(v / spec.every - Math.round(v / spec.every)) < 0.01;
      if (show) {
        const lp = polar(54, a);
        const t = svgEl('text', {
          x: lp[0].toFixed(1), y: lp[1].toFixed(1), 'text-anchor': 'middle',
          'dominant-baseline': 'middle', fill: '#1c1814', 'font-size': '9',
          'font-family': 'Liberation Sans, DejaVu Sans, sans-serif'
        });
        t.textContent = String(Math.round(v));
        svg.appendChild(t);
      }
    }
    const needle = svgEl('g', { class: 'needle', id: 'needle-' + spec.id });
    needle.appendChild(svgEl('polygon', { points: '100,36 103.4,100 100,108 96.6,100', fill: '#b42318' }));
    needle.appendChild(svgEl('circle', { cx: 100, cy: 100, r: 7, fill: '#241e18' }));
    needle.appendChild(svgEl('circle', { cx: 100, cy: 100, r: 3, fill: '#f0e2c4' }));
    svg.appendChild(needle);
    card.appendChild(svg);
    const cap = document.createElement('div');
    cap.className = 'cap';
    cap.id = 'cap-' + spec.id;
    cap.textContent = spec.title + (spec.dead ? ' · NOT METERED' : (spec.est ? ' · EST.' : ''));
    const win = document.createElement('div');
    win.className = 'win';
    win.id = 'win-' + spec.id;
    win.textContent = '—';
    card.appendChild(cap);
    card.appendChild(win);
    root.appendChild(card);
    meters[spec.id] = spec;
  });
}
function setNeedle(id, value) {
  const spec = meters[id];
  const n = document.getElementById('needle-' + id);
  if (!n || value == null || isNaN(value)) return;
  n.style.transform = 'rotate(' + deg(value, spec).toFixed(2) + 'deg)';
}
function setWin(id, text, sub) {
  const w = document.getElementById('win-' + id);
  w.textContent = text;
  if (sub) {
    const s = document.createElement('small');
    s.textContent = sub;
    w.appendChild(s);
  }
}
function lamp(id, mode) {
  const el = document.getElementById('lamp-' + id);
  el.className = 'jewel' + (mode ? ' ' + mode : '');
}
function flagsOf(status) {
  return (status || '').split(' ').filter(Boolean);
}
function paint(d, stale, err) {
  const flags = flagsOf(d && d.status);
  const onBat = flags.indexOf('OB') >= 0;
  const low = flags.indexOf('LB') >= 0;
  const chrg = flags.indexOf('CHRG') >= 0;
  const ol = flags.indexOf('OL') >= 0;
  lamp('ol', stale ? '' : (onBat ? 'on-r' : (ol ? 'on-g' : '')));
  lamp('bat', stale ? '' : (low ? 'on-r' : (onBat ? 'on-a' : 'on-g')));
  lamp('chrg', chrg && !stale ? 'on-a' : '');
  lamp('lb', low && !stale ? 'on-r blink' : '');
  lamp('fsd', d && d.fsd && d.fsd !== 'idle' ? 'on-r blink' : '');
  lamp('link', stale ? 'on-r' : 'on-g');
  if (d) {
    setNeedle('vin', d.vin);
    setNeedle('vbat', d.vbat);
    setNeedle('soc', d.soc);
    setNeedle('min', d.minutes);
    setNeedle('w', d.watts != null ? d.watts : 55);
    setWin('vin', (d.vin != null ? Number(d.vin).toFixed(0) : '—') + ' V');
    setWin('vbat', (d.vbat != null ? Number(d.vbat).toFixed(1) : '—') + ' V');
    setWin('soc', (d.soc != null ? d.soc : '—') + ' %', chrg ? 'charge reads high' : 'from voltage');
    const dead = d.meter_dead !== false;
    const cap = document.getElementById('cap-w');
    if (cap) cap.textContent = 'WATTS' + (dead ? ' · NOT METERED' : '');
    if (d.model) document.querySelector('h1').textContent = String(d.model).toUpperCase();
    setWin('min', (d.minutes != null ? d.minutes : '—') + ' min', 'at ' + (d.watts != null ? Number(d.watts).toFixed(0) : '—') + ' W');
    setWin('w', (d.watts != null ? Number(d.watts).toFixed(0) : '—') + ' W', dead ? 'UPS meter = 0' : 'measured');
    document.getElementById('clock').textContent = d.ts ? d.ts.replace('T', ' ').replace('+00:00', 'Z') : '—';
  }
  const note = document.getElementById('note');
  const extra = document.getElementById('extra');
  note.className = stale ? 'bad' : '';
  if (stale) {
    note.textContent = err || 'link down, holding last reading';
  } else if (d) {
    note.textContent = (d.status || '—') + ' · FSD ' + (d.fsd || '—') + '. ' + (d.note || '');
  }
  extra.textContent = '';
  if (d && d.vin >= 253) extra.textContent = 'Mains at the top of the band (253 V is the limit).';
}
async function tick() {
  const ctl = new AbortController();
  const timer = setTimeout(function () { ctl.abort(); }, 4000);
  try {
    const res = await fetch('/json', { cache: 'no-store', signal: ctl.signal });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const d = await res.json();
    if (!d.ok) throw new Error(d.error || 'no reading');
    last = d;
    paint(d, false);
  } catch (e) {
    paint(last, true, last ? 'link down, holding last reading' : (e && e.name === 'AbortError' ? 'timeout /json' : 'page got no /json'));
  } finally {
    clearTimeout(timer);
  }
}
build();
tick();
setInterval(tick, 5000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/json":
            with lock:
                body = json.dumps(snap).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        return


def self_check() -> None:
    assert soc_est(21.0) == 0
    assert soc_est(25.2) == 85
    assert soc_est(25.8) == 100
    assert soc_est(25.7) > 85
    parsed = parse_upsc("battery.voltage: 25.2\ninput.voltage: 253\nups.status: OL CHRG\n")
    assert parsed["ups.status"] == "OL CHRG"
    assert 70 <= minutes_est(85) <= 100
    assert watts_of({"output.L1.realpower": "0"})[1] is True
    assert watts_of({"ups.realpower": "40"}) == (40.0, False)
    assert alert_for(set(), {"OB"}) and alert_for({"OB"}, {"OL"}) and alert_for(set(), {"LB"})
    assert alert_for({"OL"}, {"OL", "CHRG"}) is None
    print("check ok", minutes_est(85))


def loop() -> None:
    state = load_state()
    last_minute = None
    while True:
        now = time.time()
        try:
            data = read_ups()
            flags = set(data["ups.status"].split())
            prev = set(state.get("flags") or [])
            if state.get("seeded"):
                text = alert_for(prev, flags)
                if text:
                    send_telegram(text)
            if "OB" in flags and not state.get("onbatt_since"):
                state["onbatt_since"] = now
            if "OB" not in flags:
                state["onbatt_since"] = None
            state["flags"] = sorted(flags)
            state["seeded"] = True
            save_state(state)
            publish(data, state, now)
            minute = int(now // 60)
            if minute != last_minute:
                with lock:
                    row = dict(snap)
                append_log(row["ts"], row["status"], row["vbat"], row["vin"], row["soc"])
                last_minute = minute
        except Exception as exc:
            with lock:
                snap["ok"] = False
                snap["error"] = str(exc)
            print("poll failed", type(exc).__name__, flush=True)
        time.sleep(10)


def main() -> None:
    import sys
    if "--check" in sys.argv:
        self_check()
        return
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"listening {BIND}:{PORT}", flush=True)
    loop()


if __name__ == "__main__":
    main()
