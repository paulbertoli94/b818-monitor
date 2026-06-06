import json
import os
from datetime import datetime, timezone
import struct
import time
import zlib
import re
from fastapi import FastAPI, Header
from fastapi.responses import HTMLResponse, JSONResponse, Response

from huawei_lte_api.Connection import Connection
from huawei_lte_api.api.Device import Device
from huawei_lte_api.api.Net import Net
from huawei_lte_api.api.Monitoring import Monitoring

ROUTER_HOST = os.getenv("ROUTER_HOST", "")
ROUTER_USER = os.getenv("ROUTER_USER", "")
ROUTER_PASSWORD = os.getenv("ROUTER_PASSWORD", "")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
ROUTER_TIMEOUT = float(os.getenv("ROUTER_TIMEOUT", "3"))

app = FastAPI()

APP_NAME = "Vodafone FWA Monitor"
PWA_BG_COLOR = "#050915"
PWA_THEME_COLOR = "#0f172a"
ICON_SIZES = {32, 180, 192, 512}

SW_JS = """
const CACHE_NAME = 'vodafone-fwa-monitor-v3';
const APP_SHELL = ['/', '/manifest.webmanifest', '/icon-180.png', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(request));
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put('/', copy));
        return response;
      }).catch(() => caches.match('/') || caches.match(request))
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        if (!response || response.status !== 200 || response.type === 'opaque') return response;
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        return response;
      });
    })
  );
});
""".strip()

FAVICON_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#1d4ed8"/>
      <stop offset="100%" stop-color="#0f172a"/>
    </linearGradient>
  </defs>
  <rect width="64" height="64" rx="16" fill="url(#bg)"/>
  <text x="32" y="41" text-anchor="middle" font-size="30">📡</text>
</svg>
""".strip()

# Stato per calcolare velocità (delta byte / delta tempo)
_last = {
    "ts": None,
    "rx": None,
    "tx": None,
    "rx_rate_bps": 0.0,
    "tx_rate_bps": 0.0,
    "rx_rate_smooth": 0.0,
    "tx_rate_smooth": 0.0,
}

def _get_stats():
    url = f"http://{ROUTER_HOST}/"

    # Se non hai password, prova senza login (molti firmware lo permettono)
    if not ROUTER_PASSWORD:
        with Connection(url, timeout=ROUTER_TIMEOUT) as conn:
            mon = Monitoring(conn)
            s = mon.traffic_statistics()
            return _extract_rx_tx(s), _extract_rx_tx(s, upload=True), s

    # Altrimenti usa login
    with Connection(url, username=ROUTER_USER or None, password=ROUTER_PASSWORD, timeout=ROUTER_TIMEOUT) as conn:
        mon = Monitoring(conn)
        s = mon.traffic_statistics()
        return _extract_rx_tx(s), _extract_rx_tx(s, upload=True), s


def _extract_rx_tx(s: dict, upload: bool = False) -> int:
    keys = ["CurrentUpload", "TotalUpload"] if upload else ["CurrentDownload", "TotalDownload"]
    for k in keys:
        v = s.get(k)
        if v is not None:
            try:
                return int(str(v).strip())
            except Exception:
                pass
    return 0

def _extract_rate(s: dict, upload: bool = False) -> float | None:
    """Preferisce i rate forniti dal router se presenti (assunti già in bit/s)."""
    keys = ["CurrentUploadRate", "UploadRate"] if upload else ["CurrentDownloadRate", "DownloadRate"]
    for k in keys:
        v = s.get(k)
        if v is None:
            continue
        try:
            val = float(str(v).strip())
            if val >= 0:
                return val
        except Exception:
            continue
    return None

def _safe_dict(payload) -> dict:
    return payload if isinstance(payload, dict) else {}

def _pick(payload: dict, *names):
    if not isinstance(payload, dict):
        return None
    normalized = {}
    for key, value in payload.items():
        if key is None:
            continue
        k = str(key)
        normalized[k] = value
        normalized[k.lower()] = value
        normalized[re.sub(r"[^a-z0-9]", "", k.lower())] = value

    for name in names:
        for candidate in (str(name), str(name).lower(), re.sub(r"[^a-z0-9]", "", str(name).lower())):
            value = normalized.get(candidate)
            if value is None or str(value) == "":
                continue
            return str(value).strip()
    return None

def _norm_antenna(value):
    if value is None or str(value).strip() == "":
        return "--"
    return "EXT" if str(value).strip() == "1" else "INT"

def _decode_lte_bands(mask):
    if mask is None:
        return ""
    raw = str(mask).strip()
    if not raw:
        return ""
    if raw.upper() == "AUTO":
        return "AUTO"
    try:
        value = int(raw, 16)
    except ValueError:
        if raw.isdigit():
            value = int(raw, 10)
        else:
            return ""
    if value <= 0:
        return ""
    return "+".join([f"B{i + 1}" for i in range(90) if (value & (1 << i))])

def _parse_enbid(cell_id):
    if cell_id is None:
        return None
    value = str(cell_id).strip()
    if not value:
        return None
    if "-" in value:
        left = value.split("-", 1)[0]
        try:
            return str(int(left))
        except ValueError:
            return None
    try:
        number = int(value)
    except ValueError:
        return None
    hex_value = f"{number:x}"
    if len(hex_value) <= 2:
        return None
    base = hex_value[:-2]
    if not base:
        return None
    try:
        return str(int(base, 16))
    except ValueError:
        return None

def _normalize_plmn(plmn, enbid):
    if plmn is None:
        return None
    p = str(plmn).strip()
    if p == "22201":
        return "2221"
    if p == "22299":
        return "22288"
    if p == "22250" and enbid is not None and len(str(enbid)) == 6:
        return "22288"
    return p

def _signal_payload(router_password: str | None = None):
    if not ROUTER_HOST:
        return {"error": "ROUTER_HOST non configurato", "timestamp": datetime.now(timezone.utc).isoformat()}

    url = f"http://{ROUTER_HOST}/"
    password = router_password or ROUTER_PASSWORD or None
    partial_errors = {}
    try:
        with Connection(url, username=ROUTER_USER or None, password=password, timeout=ROUTER_TIMEOUT) as conn:
            monitor = Monitoring(conn)
            net = Net(conn)
            device = Device(conn)

            def read_part(name, fn):
                try:
                    return _safe_dict(fn())
                except Exception as exc:
                    partial_errors[name] = str(exc)
                    return {}

            raw_signal = read_part("signal", device.signal)
            raw_antenna = read_part("antenna_type", device.antenna_type)
            raw_net_mode = read_part("net_mode", net.net_mode)
            raw_status = read_part("status", monitor.status)
    except Exception as exc:
        return {"error": str(exc), "timestamp": datetime.now(timezone.utc).isoformat()}

    enbid = _parse_enbid(_pick(raw_signal, "cell_id", "cell-id", "cellid"))
    plmn = _pick(raw_signal, "plmn")
    lte_band = _pick(raw_net_mode, "LTEBand", "lteband")

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rsrp": _pick(raw_signal, "rsrp"),
        "rsrq": _pick(raw_signal, "rsrq"),
        "sinr": _pick(raw_signal, "sinr"),
        "rssi": _pick(raw_signal, "rssi"),
        "nrrsrp": _pick(raw_signal, "nrrsrp"),
        "nrrsrq": _pick(raw_signal, "nrrsrq"),
        "nrsinr": _pick(raw_signal, "nrsinr"),
        "dlbandwidth": _pick(raw_signal, "dlbandwidth", "dlBandwidth", "dl_bandwidth"),
        "ulbandwidth": _pick(raw_signal, "ulbandwidth", "ulBandwidth", "ul_bandwidth"),
        "band": _pick(raw_signal, "band"),
        "cell_id": _pick(raw_signal, "cell_id", "cell-id", "cellid"),
        "plmn": plmn,
        "enbid": enbid,
        "mode": "4G+" if _pick(raw_status, "CurrentNetworkTypeEx") == "1011" else "--",
        "signal_icon": _pick(raw_status, "SignalIcon"),
        "signal_strength": _pick(raw_status, "SignalStrength"),
        "antenna1": _norm_antenna(_pick(raw_antenna, "antenna1type", "antenna1Type")),
        "antenna2": _norm_antenna(_pick(raw_antenna, "antenna2type", "antenna2Type")),
        "allowed": _decode_lte_bands(lte_band),
        "lteitaly": f"https://lteitaly.it/internal/map.php#bts={_normalize_plmn(plmn, enbid)}.{enbid}" if enbid else None,
        "raw_signal": raw_signal,
        "raw_antenna": raw_antenna,
        "raw_net_mode": raw_net_mode,
        "raw_status": raw_status,
        "partial_errors": partial_errors,
    }

def _update_rates(rx, tx, router_rx_rate: float | None = None, router_tx_rate: float | None = None):
    now = time.time()
    if _last["ts"] is None:
        _last.update({
            "ts": now,
            "rx": rx,
            "tx": tx,
            "rx_rate_bps": 0.0,
            "tx_rate_bps": 0.0,
            "rx_rate_smooth": 0.0,
            "tx_rate_smooth": 0.0,
        })
        return

    dt = now - _last["ts"]
    if dt <= 0:
        return

    drx = max(0, rx - (_last["rx"] or rx))
    dtx = max(0, tx - (_last["tx"] or tx))

    if router_rx_rate is not None:
        # Alcuni modem espongono CurrentDownloadRate in byte/s: converti in bit/s
        rx_rate = float(router_rx_rate) * 8.0
    else:
        rx_rate = (drx * 8.0) / dt
        if drx == 0:
            rx_rate = _last["rx_rate_bps"] * 0.85

    if router_tx_rate is not None:
        tx_rate = float(router_tx_rate) * 8.0
    else:
        tx_rate = (dtx * 8.0) / dt
        if dtx == 0:
            tx_rate = _last["tx_rate_bps"] * 0.85

    _last["rx_rate_bps"] = rx_rate
    _last["tx_rate_bps"] = tx_rate

    # Smoothing esponenziale per ridurre oscillazioni (es. POLL_SECONDS=1)
    alpha = 0.6
    _last["rx_rate_smooth"] = rx_rate if _last["rx_rate_smooth"] == 0 else (_last["rx_rate_smooth"] * (1 - alpha) + rx_rate * alpha)
    _last["tx_rate_smooth"] = tx_rate if _last["tx_rate_smooth"] == 0 else (_last["tx_rate_smooth"] * (1 - alpha) + tx_rate * alpha)
    _last["ts"] = now
    _last["rx"] = rx
    _last["tx"] = tx

def _human_mbps(bps: float) -> float:
    # Mbps decimali (base 10) per allineare ai contatori della maggior parte delle linee
    return round(bps / 1_000_000, 2)

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack("!I", len(data))
        + tag
        + data
        + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )

def _generate_icon_png(size: int) -> bytes:
    bg = (229, 27, 35, 255)
    fg = (255, 255, 255, 255)
    rows = []
    baseline = int(size * 0.76)
    bar_width = max(10, size // 10)
    gap = max(8, size // 18)
    left = (size - (bar_width * 3 + gap * 2)) // 2
    heights = [0.20, 0.36, 0.52]

    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            pixel = bg
            for index, height_ratio in enumerate(heights):
                x0 = left + index * (bar_width + gap)
                x1 = x0 + bar_width
                y0 = baseline - int(size * height_ratio)
                if x0 <= x < x1 and y0 <= y < baseline:
                    pixel = fg
                    break
            row.extend(pixel)
        rows.append(bytes(row))

    raw = b"".join(rows)
    ihdr = struct.pack("!IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"".join([
        b"\x89PNG\r\n\x1a\n",
        _png_chunk(b"IHDR", ihdr),
        _png_chunk(b"IDAT", zlib.compress(raw, level=9)),
        _png_chunk(b"IEND", b""),
    ])

@app.get("/api/speed")
def api_speed():
    try:
        rx, tx, raw = _get_stats()
        _update_rates(rx, tx, _extract_rate(raw), _extract_rate(raw, upload=True))
        return JSONResponse({
            "router": ROUTER_HOST,
            "download_mbps": _human_mbps(_last["rx_rate_smooth"]),
            "upload_mbps": _human_mbps(_last["tx_rate_smooth"]),
            "poll_seconds": POLL_SECONDS,
            "raw": raw,  # utile per debug: puoi toglierlo dopo
        })
    except Exception as exc:
        return JSONResponse({
            "router": ROUTER_HOST,
            "download_mbps": None,
            "upload_mbps": None,
            "poll_seconds": POLL_SECONDS,
            "error": str(exc),
        })

@app.get("/api/signal")
def api_signal(x_router_password: str | None = Header(default=None)):
    return JSONResponse(_signal_payload(x_router_password))

@app.get("/manifest.webmanifest")
def manifest():
    payload = {
        "name": APP_NAME,
        "short_name": "FWA Monitor",
        "description": "Monitor live della velocita Vodafone FWA installabile su telefono e desktop.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": PWA_BG_COLOR,
        "theme_color": PWA_THEME_COLOR,
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return Response(content=json.dumps(payload), media_type="application/manifest+json")

@app.get("/sw.js")
def service_worker():
    return Response(
        content=SW_JS,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )

@app.get("/icon-{size}.png")
def app_icon(size: int):
    if size not in ICON_SIZES:
        return Response(status_code=404)
    return Response(content=_generate_icon_png(size), media_type="image/png")

@app.get("/favicon.svg")
def favicon():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{APP_NAME}</title>
  <meta name="application-name" content="{APP_NAME}"/>
  <meta name="apple-mobile-web-app-capable" content="yes"/>
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
  <meta name="apple-mobile-web-app-title" content="FWA Monitor"/>
  <meta name="mobile-web-app-capable" content="yes"/>
  <meta name="theme-color" content="{PWA_THEME_COLOR}"/>
  <meta name="description" content="Monitor live della velocita Vodafone FWA con installazione PWA su telefono e desktop."/>
  <link rel="manifest" href="/manifest.webmanifest"/>
  <link rel="apple-touch-icon" href="/icon-180.png"/>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg"/>
  <link rel="icon" type="image/png" sizes="32x32" href="/icon-32.png"/>
  <link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png"/>
  <style>
    :root {{
      color-scheme: dark;
      --bg: radial-gradient(circle at 20% 20%, #0f172a 0%, #0b1220 40%, #050915 75%);
      --text: #e5e7eb;
      --card: linear-gradient(145deg, rgba(17, 24, 39, 0.7), rgba(15, 23, 42, 0.9));
      --border: rgba(59, 130, 246, 0.25);
      --muted: #9ca3af;
      --small: #6b7280;
      --legend: #cbd5e1;
      --chart-bg: #0b1220;
      --grid: #1f2937;
      --status-ok-bg: rgba(74, 222, 128, 0.14);
      --status-ok-border: rgba(74, 222, 128, 0.35);
      --status-ok-text: #bbf7d0;
      --status-err-bg: rgba(248, 113, 113, 0.12);
      --status-err-border: rgba(248, 113, 113, 0.35);
      --status-err-text: #fecdd3;
      --pill-bg: rgba(96, 165, 250, 0.15);
      --pill-border: rgba(96, 165, 250, 0.35);
      --pill-text: #93c5fd;
    }}
    body.light {{
      color-scheme: light;
      --bg: linear-gradient(135deg, #e0f2fe 0%, #e2e8f0 40%, #f8fafc 100%);
      --text: #0f172a;
      --card: #ffffff;
      --border: rgba(59, 130, 246, 0.18);
      --muted: #4b5563;
      --small: #6b7280;
      --legend: #475569;
      --chart-bg: #f8fafc;
      --grid: #e5e7eb;
      --status-ok-bg: rgba(16, 185, 129, 0.15);
      --status-ok-border: rgba(16, 185, 129, 0.35);
      --status-ok-text: #0f766e;
      --status-err-bg: rgba(248, 113, 113, 0.12);
      --status-err-border: rgba(248, 113, 113, 0.35);
      --status-err-text: #b91c1c;
      --pill-bg: rgba(59, 130, 246, 0.1);
      --pill-border: rgba(59, 130, 246, 0.25);
      --pill-text: #1d4ed8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: "Segoe UI", "SF Pro Display", system-ui, -apple-system, Roboto, sans-serif;
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 32px;
      background: var(--bg);
      color: var(--text);
      transition: background 0.3s ease, color 0.3s ease;
    }}
    .shell {{
      width: min(720px, 100%);
    }}
    .headline {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      gap: 12px;
    }}
    .title {{
      font-size: 22px;
      font-weight: 700;
      letter-spacing: 0.3px;
      display: flex;
      gap: 12px;
      align-items: center;
    }}
    .actions {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .pill {{
      padding: 4px 10px;
      border-radius: 999px;
      background: var(--pill-bg);
      color: var(--pill-text);
      font-size: 12px;
      border: 1px solid var(--pill-border);
    }}
    .toggle-btn {{
      border: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.04);
      color: var(--text);
      padding: 6px 12px;
      border-radius: 12px;
      font-size: 13px;
      cursor: pointer;
      transition: all 0.2s ease;
      backdrop-filter: blur(4px);
    }}
    .toggle-btn:hover {{
      transform: translateY(-1px);
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.15);
    }}
    .toggle-btn[hidden] {{ display: none; }}
    .install-row {{ display: flex; justify-content: flex-end; margin-top: 8px; }}
    .install-row .toggle-btn {{ font-size: 11px; padding: 4px 8px; opacity: 0.78; }}
    .card {{
      padding: 20px 22px;
      border-radius: 18px;
      background: var(--card);
      box-shadow: 0 24px 60px rgba(0, 0, 0, 0.35);
      border: 1px solid var(--border);
      backdrop-filter: blur(10px);
    }}
    .row {{ display: flex; justify-content: space-between; margin: 12px 0; align-items: center; }}
    .big {{ font-size: 38px; font-weight: 800; color: var(--text); }}
    .muted {{ color: var(--muted); font-size: 13px; }}
    .small {{ font-size: 12px; color: var(--small); }}
    .chart-wrap {{ margin-top: 18px; }}
    #chart {{ width: 100%; height: 240px; display: block; }}
    .legend {{ display: flex; gap: 14px; align-items: center; font-size: 12px; color: var(--legend); margin-top: 8px; flex-wrap: wrap; }}
    .dot {{ width: 12px; height: 12px; border-radius: 50%; display: inline-block; margin-right: 6px; }}
    .dl {{ background: #60a5fa; color: #60a5fa; }}
    .ul {{ background: #34d399; color: #34d399; }}
    .chart-bg {{ fill: var(--chart-bg); }}
    .grid {{ stroke: var(--grid); stroke-width: 1; }}
    .line {{ fill: none; stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }}
    .line.dl {{ stroke: url(#gradDl); }}
    .line.ul {{ stroke: url(#gradUl); }}
    .label {{ fill: #94a3b8; font-size: 10px; text-anchor: end; }}
    .signal-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 12px; }}
    .signal-strip {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 10px; color: var(--legend); font-size: 12px; }}
    .signal-strip b {{ color: var(--text); font-weight: 700; }}
    .signal-strip a {{ color: var(--pill-text); text-decoration: none; font-weight: 700; }}
    .signal-strip a:hover {{ text-decoration: underline; }}
    .kv {{ border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; background: rgba(255,255,255,0.02); min-width: 0; }}
    .kv[data-metric] {{ cursor: pointer; }}
    .kv[data-metric]:hover {{ border-color: var(--pill-border); background: var(--pill-bg); }}
    .kv .lbl {{ font-size: 11px; color: var(--small); margin-bottom: 4px; }}
    .kv .val {{ font-weight: 800; font-size: 18px; color: var(--text); margin-bottom: 5px; }}
    .kv .val a {{ color: var(--pill-text); text-decoration: none; }}
    .kv .val a:hover {{ text-decoration: underline; }}
    .signal-tools {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 12px 0 6px 0; }}
    .signal-tools .toggle-btn {{ border: 1px solid var(--border); border-radius: 10px; padding: 5px 10px; font-size: 12px; }}
    .signal-tools .toggle-btn.active {{ background: var(--pill-bg); border-color: var(--pill-border); color: var(--pill-text); }}
    .radio-actions {{ justify-content: flex-start; margin-top: 12px; }}
    .radio-actions .toggle-btn {{ opacity: 1; }}
    .radio-actions .toggle-btn.secondary {{ opacity: 0.72; }}
    .network-badge {{ display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--pill-border); border-radius: 999px; padding: 5px 10px; background: var(--pill-bg); color: var(--text); font-size: 12px; font-weight: 800; }}
    .network-bars {{ color: #34d399; letter-spacing: 1px; }}
    .login-state {{ color: var(--small); font-size: 12px; min-height: 16px; margin-top: 8px; }}
    .radio-panel[hidden] {{ display: none; }}
    .signal-chart {{ width: 100%; height: 36px; }}
    .signal-chart svg {{ width: 100%; height: 100%; }}
    .sig-hidden {{ display: none !important; }}
    .signal-legend {{ margin-top: 4px; display: flex; gap: 8px; align-items: center; font-size: 11px; color: var(--legend); flex-wrap: wrap; }}
    .signal-legend-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
    .signal-legend-dot.ok {{ background: #22c55e; }}
    .signal-legend-dot.warn {{ background: #eab308; }}
    .signal-legend-dot.bad {{ background: #f97316; }}
    .metric-modal {{ position: fixed; inset: 0; z-index: 50; display: grid; place-items: center; padding: 18px; background: rgba(2, 6, 23, 0.72); }}
    .metric-modal[hidden] {{ display: none; }}
    .metric-panel {{ width: min(900px, 100%); border: 1px solid var(--border); border-radius: 10px; background: #0f172a; box-shadow: 0 28px 80px rgba(0,0,0,0.45); padding: 16px; }}
    body.light .metric-panel {{ background: #ffffff; }}
    .metric-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: start; margin-bottom: 12px; }}
    .metric-title {{ font-size: 18px; font-weight: 800; }}
    .metric-stats {{ display: flex; gap: 12px; flex-wrap: wrap; color: var(--legend); font-size: 12px; margin-top: 4px; }}
    .metric-stats b {{ color: var(--text); }}
    #metricChart {{ width: 100%; height: 320px; display: block; }}
    .status {{
      padding: 6px 10px;
      border-radius: 10px;
      background: var(--status-ok-bg);
      color: var(--status-ok-text);
      font-size: 12px;
      border: 1px solid var(--status-ok-border);
      transition: all 0.2s ease;
    }}
    .status.err {{
      background: var(--status-err-bg);
      color: var(--status-err-text);
      border-color: var(--status-err-border);
    }}
    .foot {{
      margin-top: 10px;
      min-height: 16px;
      font-size: 12px;
      color: #fca5a5;
      opacity: 0.9;
    }}
    .foot.hide {{ opacity: 0; transition: opacity 0.2s ease; }}
    @media (max-width: 720px) {{
      body {{
        padding: 16px;
        align-items: center;
      }}
      .shell {{
        display: flex;
        flex-direction: column;
        justify-content: center;
        min-height: calc(100vh - 32px);
      }}
      .headline {{
        flex-direction: column;
        align-items: stretch;
      }}
      .actions {{
        justify-content: space-between;
        flex-wrap: wrap;
      }}
      .card {{
        padding: 18px;
      }}
      .big {{
        font-size: 30px;
      }}
      .signal-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="headline">
      <div class="title">
        <span>📡 Vodafone FWA Monitor</span>
        <span class="pill">Live</span>
      </div>
      <div class="actions">
        <div class="muted">Router: {ROUTER_HOST}</div>
        <span class="network-badge" id="networkBadge" hidden><span id="sig-network-mode">—</span><span class="network-bars" id="sig-bars">—</span></span>
        <button class="toggle-btn" id="themeToggle" title="Cambia tema">🌙</button>
      </div>
    </div>
    <div class="card">
      <div class="row"><div>Download</div><div class="big" id="down">—</div></div>
      <div class="row"><div>Upload</div><div class="big" id="up">—</div></div>
      <div class="status" id="status">Aggiornamento…</div>
      <div class="chart-wrap">
        <div class="muted">Andamento (Mbps)</div>
        <svg id="chart" preserveAspectRatio="none"></svg>
        <div class="legend">
          <span class="dot dl"></span><span>Download</span>
          <span class="dot ul"></span><span>Upload</span>
        </div>
      </div>
      <div class="small">Intervallo: {POLL_SECONDS}s</div>
      <div class="signal-tools">
        <button class="toggle-btn" id="showRadioBtn" type="button">Mostra dati radio</button>
      </div>
      <div class="radio-panel" id="radioPanel" hidden>
        <div class="signal-tools radio-actions" id="signalTools">
          <button class="toggle-btn" id="hideRadioBtn" type="button">Nascondi</button>
          <button class="toggle-btn secondary" id="routerForgetBtn" type="button">Dimentica accesso</button>
        </div>
        <div class="login-state" id="routerLoginState">Dati radio pronti.</div>
        <div class="signal-grid" id="signalSummary">
          <div class="kv signal-optional sig-lte-only" data-metric="rsrp">
            <div class="lbl">RSRP</div>
            <div class="val" id="sig-rsrp">—</div>
            <div class="signal-chart" id="chart-rsrp"></div>
          </div>
          <div class="kv signal-optional sig-lte-only" data-metric="rsrq">
            <div class="lbl">RSRQ</div>
            <div class="val" id="sig-rsrq">—</div>
            <div class="signal-chart" id="chart-rsrq"></div>
          </div>
          <div class="kv signal-optional sig-lte-only" data-metric="sinr">
            <div class="lbl">SINR</div>
            <div class="val" id="sig-sinr">—</div>
            <div class="signal-chart" id="chart-sinr"></div>
          </div>
          <div class="kv signal-optional sig-nr-only" data-metric="nrrsrp">
            <div class="lbl">NR RSRP</div>
            <div class="val" id="sig-nrrsrp">—</div>
            <div class="signal-chart" id="chart-nrrsrp"></div>
          </div>
          <div class="kv signal-optional sig-nr-only" data-metric="nrrsrq">
            <div class="lbl">NR RSRQ</div>
            <div class="val" id="sig-nrrsrq">—</div>
            <div class="signal-chart" id="chart-nrrsrq"></div>
          </div>
          <div class="kv signal-optional sig-nr-only" data-metric="nrsinr">
            <div class="lbl">NR SINR</div>
            <div class="val" id="sig-nrsinr">—</div>
            <div class="signal-chart" id="chart-nrsinr"></div>
          </div>
        </div>
        <div class="signal-strip">
          <span>RSSI <b id="sig-rssi">—</b></span>
          <span>Ant <b id="sig-ant">—</b></span>
          <span>Cell <b id="sig-cell-id">—</b></span>
          <span>ENB <b><a id="sig-lteitaly" href="#" target="lteitaly">—</a></b></span>
          <span>Band <b id="sig-band">—</b></span>
          <span>BW <b id="sig-bw">—</b></span>
          <span>PLMN <b id="sig-plmn">—</b></span>
          <span>Allowed <b id="sig-allowed">—</b></span>
        </div>
        <div class="signal-legend">
          <span><span class="signal-legend-dot ok"></span>Buono</span>
          <span><span class="signal-legend-dot warn"></span>Medio</span>
          <span><span class="signal-legend-dot bad"></span>Basso</span>
        </div>
      </div>
      <div class="foot hide" id="errorFoot"></div>
    </div>
    <div class="install-row">
      <button class="toggle-btn" id="installBtn" hidden>Installa app</button>
    </div>
  </div>
  <div class="metric-modal" id="metricModal" hidden>
    <div class="metric-panel">
      <div class="metric-head">
        <div>
          <div class="metric-title" id="metricTitle">—</div>
          <div class="metric-stats">
            <span>Ultimo <b id="metricLast">—</b></span>
            <span>Media <b id="metricAvg">—</b></span>
            <span>Campioni <b id="metricCount">0</b></span>
          </div>
        </div>
        <button class="toggle-btn" id="metricClose" type="button">Chiudi</button>
      </div>
      <svg id="metricChart" preserveAspectRatio="none"></svg>
    </div>
  </div>

<script>
const pollMs = {int(POLL_SECONDS*1000)};
const maxPoints = Math.max(30, Math.floor(180000 / pollMs)); // ~3 minuti di storico
const signalMaxPoints = 80;
const history = [];
const signalHistory = [];
let activeMetric = null;
let timer = null;
let paused = false;
let deferredInstallPrompt = null;
let routerPassword = '';
let radioEnabled = false;
let authCooldownUntil = 0;

function isStandalone() {{
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}}

function updateInstallButton() {{
  const btn = document.getElementById('installBtn');
  if (!btn) return;
  btn.hidden = isStandalone() || deferredInstallPrompt === null;
}}

function applyTheme(theme) {{
  document.body.classList.toggle('light', theme === 'light');
  const btn = document.getElementById('themeToggle');
  if (btn) btn.textContent = theme === 'light' ? '🌙' : '☀️';
  try {{ localStorage.setItem('theme', theme); }} catch (_) {{}}
}}

const savedTheme = (typeof localStorage !== 'undefined' && localStorage.getItem('theme') === 'light') ? 'light' : 'dark';
applyTheme(savedTheme);

const toggleEl = document.getElementById('themeToggle');
if (toggleEl) {{
  toggleEl.addEventListener('click', () => {{
    const next = document.body.classList.contains('light') ? 'dark' : 'light';
    applyTheme(next);
  }});
}}

const installBtn = document.getElementById('installBtn');
if (installBtn) {{
  installBtn.addEventListener('click', async () => {{
    if (!deferredInstallPrompt) return;
    deferredInstallPrompt.prompt();
    try {{
      await deferredInstallPrompt.userChoice;
    }} finally {{
      deferredInstallPrompt = null;
      updateInstallButton();
    }}
  }});
}}

window.addEventListener('beforeinstallprompt', (event) => {{
  event.preventDefault();
  deferredInstallPrompt = event;
  updateInstallButton();
}});

window.addEventListener('appinstalled', () => {{
  deferredInstallPrompt = null;
  updateInstallButton();
}});

const routerForgetBtn = document.getElementById('routerForgetBtn');
const showRadioBtn = document.getElementById('showRadioBtn');
const hideRadioBtn = document.getElementById('hideRadioBtn');
if (showRadioBtn) {{
  showRadioBtn.addEventListener('click', () => {{
    if (ensureRouterPassword()) {{
      setRadioEnabled(true);
    }}
  }});
}}
if (hideRadioBtn) {{
  hideRadioBtn.addEventListener('click', () => setRadioEnabled(false));
}}
if (routerForgetBtn) {{
  routerForgetBtn.addEventListener('click', () => {{
    saveRouterPassword('');
    setRadioEnabled(false);
    renderSignal({{ error: 'Accesso router dimenticato.' }});
  }});
}}

if ('serviceWorker' in navigator) {{
  window.addEventListener('load', () => {{
    navigator.serviceWorker.register('/sw.js').catch((error) => {{
      console.error('service worker registration failed', error);
    }});
  }});
}}

const num = (n) => Number(n || 0);
const fmt = (n) => num(n).toFixed(2);
const fmtSignal = (v) => (v === null || v === undefined || v === '') ? '—' : String(v);
function fmtBars(value) {{
  const n = Number(value);
  if (!Number.isFinite(n)) return fmtSignal(value);
  const capped = Math.max(0, Math.min(5, Math.round(n)));
  return '▰'.repeat(capped) + '▱'.repeat(5 - capped);
}}
const toSignalNumber = (v) => {{
  if (v === null || v === undefined || v === '') return null;
  const match = String(v).replace(',', '.').match(/-?[0-9]+([.][0-9]+)?/);
  const parsed = match ? Number(match[0]) : NaN;
  return Number.isFinite(parsed) ? parsed : null;
}};

const metricDefs = {{
  rsrp: {{ label: 'RSRP', min: -130, max: -60, unit: 'dBm' }},
  rsrq: {{ label: 'RSRQ', min: -16, max: -3, unit: 'dB' }},
  sinr: {{ label: 'SINR', min: 0, max: 24, unit: 'dB' }},
  nrrsrp: {{ label: 'NR RSRP', min: -130, max: -60, unit: 'dBm' }},
  nrrsrq: {{ label: 'NR RSRQ', min: -16, max: -3, unit: 'dB' }},
  nrsinr: {{ label: 'NR SINR', min: 0, max: 24, unit: 'dB' }},
}};

function updateNrVisibility() {{
  const nrCharts = Array.from(document.querySelectorAll('.sig-nr-only'));
  const hasNr = signalHistory.some((point) => point.nrrsrp !== null || point.nrrsrq !== null || point.nrsinr !== null);
  nrCharts.forEach((el) => {{
    el.classList.toggle('sig-hidden', !hasNr);
  }});
}}

function loadRadioPreferences() {{
  try {{
    radioEnabled = localStorage.getItem('radioEnabled') === '1' && !!routerPassword;
  }} catch (_) {{
    radioEnabled = false;
  }}
  updateRadioPanel();
  updateNrVisibility();
}}

function setRadioEnabled(enabled) {{
  radioEnabled = !!enabled;
  try {{ localStorage.setItem('radioEnabled', radioEnabled ? '1' : '0'); }} catch (_) {{}}
  updateRadioPanel();
  if (radioEnabled) {{
    updateRouterLoginState(routerPassword ? 'Dati radio autenticati.' : 'Accesso router richiesto.');
    tick();
  }}
}}

function updateRadioPanel() {{
  const panel = document.getElementById('radioPanel');
  const showBtn = document.getElementById('showRadioBtn');
  const badge = document.getElementById('networkBadge');
  if (panel) panel.hidden = !radioEnabled;
  if (showBtn) showBtn.hidden = radioEnabled;
  if (badge && !radioEnabled) badge.hidden = true;
}}

function loadRouterPassword() {{
  try {{
    routerPassword = sessionStorage.getItem('routerPassword') || '';
  }} catch (_) {{
    routerPassword = '';
  }}
  updateRouterLoginState(routerPassword ? 'Accesso router in sessione.' : 'Accesso router non inserito.');
}}

function saveRouterPassword(value) {{
  routerPassword = value || '';
  try {{
    if (routerPassword) sessionStorage.setItem('routerPassword', routerPassword);
    else sessionStorage.removeItem('routerPassword');
  }} catch (_) {{}}
  updateRouterLoginState(routerPassword ? 'Accesso router in sessione.' : 'Accesso router dimenticato.');
}}

function ensureRouterPassword() {{
  if (Date.now() < authCooldownUntil) {{
    const seconds = Math.ceil((authCooldownUntil - Date.now()) / 1000);
    updateRouterLoginState('Riprova tra ' + seconds + 's: il router ha bloccato troppi tentativi.');
    return false;
  }}
  if (routerPassword) return true;
  const value = window.prompt('Password router');
  if (!value) {{
    updateRouterLoginState('Accesso router non inserito.');
    return false;
  }}
  saveRouterPassword(value);
  return true;
}}

function updateRouterLoginState(text) {{
  const el = document.getElementById('routerLoginState');
  if (el) el.textContent = text;
}}

function isRouterPasswordError(error) {{
  const text = String(error || '').toLowerCase();
  return text.includes('108007') || text.includes('108006') || text.includes('password') || text.includes('login');
}}

function stopRadioForPasswordError(error) {{
  authCooldownUntil = Date.now() + 60000;
  saveRouterPassword('');
  setRadioEnabled(false);
  renderSignal({{ error: 'Accesso router fallito.' }});
  updateRouterLoginState('Password errata o troppi tentativi. Riprova tra 60s.');
}}

function signalHeaders() {{
  return routerPassword ? {{ 'X-Router-Password': routerPassword }} : {{}};
}}

function drawSignalBars(containerId, rawValues, min, max) {{
  const container = document.getElementById(containerId);
  if (!container) return;

  const source = Array.isArray(rawValues) ? rawValues : [];
  const w = Math.max(1, container.clientWidth || 220);
  const h = 36;
  const padTop = 2;
  const lineW = 3;
  const gap = 2;
  const scaleMin = min;
  const scaleMax = max;
  if (!source.length) {{
    container.innerHTML = '<svg version=\"1.1\" viewBox=\"0 0 ' + w + ' ' + h + '\" width=\"' + w + '\" height=\"' + h + '\" preserveAspectRatio=\"none\"><text x=\"6\" y=\"24\" fill=\"#94a3b8\" font-size=\"11\">—</text></svg>';
    return;
  }}
  const values = source.map((v) => (Number.isFinite(v) ? v : null));
  const hasVal = values.some((v) => v !== null);
  if (!hasVal) {{
    container.innerHTML = '<svg version=\"1.1\" viewBox=\"0 0 ' + w + ' ' + h + '\" width=\"' + w + '\" height=\"' + h + '\" preserveAspectRatio=\"none\"><text x=\"6\" y=\"24\" fill=\"#94a3b8\" font-size=\"11\">—</text></svg>';
    return;
  }}
  const denom = Math.max(scaleMax - scaleMin, 1);
  const step = lineW + gap;
  let svg = '<svg version=\"1.1\" viewBox=\"0 0 ' + w + ' ' + h + '\" width=\"' + w + '\" height=\"' + h + '\" preserveAspectRatio=\"none\" style=\"display:block\">';
  for (let i = 0; i < values.length; i++) {{
    const value = values[i];
    const x = 4 + step * i;
    if (x > w - 2) break;
    const baseY = h - 1;
    let y = baseY;
    if (value !== null) {{
      const bounded = Math.max(scaleMin, Math.min(scaleMax, value));
      y = padTop + (h - padTop * 2) * (1 - ((bounded - scaleMin) / denom));
    }}
    const ratio = value === null ? 0 : ((value - scaleMin) / denom) * 100;
    let color = '#64748b';
    if (value !== null) {{
      color = ratio < 50 ? '#f97316' : (ratio < 85 ? '#eab308' : '#22c55e');
    }}
    svg += '<line x1=\"' + x.toFixed(2) + '\" y1=\"' + baseY + '\" x2=\"' + x.toFixed(2) + '\" y2=\"' + y.toFixed(2) + '\" stroke=\"' + color + '\" stroke-width=\"' + lineW + '\"></line>';
  }}
  svg += '</svg>';
  container.innerHTML = svg;
}}

function drawMetricLine(metricKey) {{
  const svg = document.getElementById('metricChart');
  const def = metricDefs[metricKey];
  if (!svg || !def) return;

  const samples = signalHistory
    .map((point) => point[metricKey])
    .filter((value) => Number.isFinite(value));

  document.getElementById('metricTitle').textContent = def.label;
  document.getElementById('metricCount').textContent = String(samples.length);

  if (!samples.length) {{
    document.getElementById('metricLast').textContent = '—';
    document.getElementById('metricAvg').textContent = '—';
    svg.innerHTML = '<text x="18" y="42" fill="#94a3b8" font-size="13">Nessun campione in sessione</text>';
    return;
  }}

  const last = samples[samples.length - 1];
  const avg = samples.reduce((sum, value) => sum + value, 0) / samples.length;
  document.getElementById('metricLast').textContent = last.toFixed(1) + def.unit;
  document.getElementById('metricAvg').textContent = avg.toFixed(1) + def.unit;

  const w = svg.clientWidth || 820;
  const h = svg.clientHeight || 320;
  const pad = 34;
  const innerW = Math.max(1, w - pad * 2);
  const innerH = Math.max(1, h - pad * 2);
  const min = def.min;
  const max = def.max;
  const denom = Math.max(1, max - min);
  const len = samples.length;

  const pointString = samples.map((value, index) => {{
    const bounded = Math.max(min, Math.min(max, value));
    const x = pad + (len <= 1 ? 0 : (index / (len - 1)) * innerW);
    const y = pad + innerH * (1 - ((bounded - min) / denom));
    return x.toFixed(1) + ',' + y.toFixed(1);
  }}).join(' ');

  const avgY = pad + innerH * (1 - ((Math.max(min, Math.min(max, avg)) - min) / denom));
  const grid = [];
  for (let i = 0; i <= 4; i++) {{
    const y = pad + (innerH / 4) * i;
    const label = (max - (denom / 4) * i).toFixed(0);
    grid.push('<line x1="' + pad + '" y1="' + y + '" x2="' + (pad + innerW) + '" y2="' + y + '" class="grid"/>');
    grid.push('<text x="' + (pad - 8) + '" y="' + (y + 4) + '" class="label">' + label + '</text>');
  }}

  svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
  svg.innerHTML =
    '<rect x="0" y="0" width="' + w + '" height="' + h + '" rx="8" class="chart-bg"/>' +
    grid.join('') +
    '<line x1="' + pad + '" y1="' + avgY.toFixed(1) + '" x2="' + (pad + innerW) + '" y2="' + avgY.toFixed(1) + '" stroke="#f59e0b" stroke-width="1.5" stroke-dasharray="5 5"/>' +
    '<polyline points="' + pointString + '" fill="none" stroke="#60a5fa" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>';
}}

function openMetricModal(metricKey) {{
  if (!metricDefs[metricKey]) return;
  activeMetric = metricKey;
  const modal = document.getElementById('metricModal');
  if (modal) modal.hidden = false;
  drawMetricLine(metricKey);
}}

function closeMetricModal() {{
  activeMetric = null;
  const modal = document.getElementById('metricModal');
  if (modal) modal.hidden = true;
}}

function setupMetricModal() {{
  document.querySelectorAll('[data-metric]').forEach((el) => {{
    el.addEventListener('click', () => openMetricModal(el.getAttribute('data-metric')));
  }});
  const close = document.getElementById('metricClose');
  const modal = document.getElementById('metricModal');
  if (close) close.addEventListener('click', closeMetricModal);
  if (modal) {{
    modal.addEventListener('click', (event) => {{
      if (event.target === modal) closeMetricModal();
    }});
  }}
}}

function renderSignalCharts() {{
  const recent = signalHistory.slice(-signalMaxPoints);
  const rsrpValues = recent.map((point) => point.rsrp);
  const rsrqValues = recent.map((point) => point.rsrq);
  const sinrValues = recent.map((point) => point.sinr);
  const nrrsrpValues = recent.map((point) => point.nrrsrp);
  const nrrsrqValues = recent.map((point) => point.nrrsrq);
  const nrsinrValues = recent.map((point) => point.nrsinr);

  drawSignalBars('chart-rsrp', rsrpValues, -130, -60);
  drawSignalBars('chart-rsrq', rsrqValues, -16, -3);
  drawSignalBars('chart-sinr', sinrValues, 0, 24);
  drawSignalBars('chart-nrrsrp', nrrsrpValues, -130, -60);
  drawSignalBars('chart-nrrsrq', nrrsrqValues, -16, -3);
  drawSignalBars('chart-nrsinr', nrsinrValues, 0, 24);
  updateNrVisibility();
  if (activeMetric) drawMetricLine(activeMetric);
}}

function addSignalPoint(payload) {{
  if (!payload || payload.error) return;
  signalHistory.push({{
    ts: Date.now(),
    rsrp: toSignalNumber(payload.rsrp),
    rsrq: toSignalNumber(payload.rsrq),
    sinr: toSignalNumber(payload.sinr),
    nrrsrp: toSignalNumber(payload.nrrsrp),
    nrrsrq: toSignalNumber(payload.nrrsrq),
    nrsinr: toSignalNumber(payload.nrsinr),
  }});
  renderSignalCharts();
}}

function renderSignal(payload) {{
  const set = (id, value) => {{
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = fmtSignal(value);
  }};

  if (!payload || payload.error) {{
    ['sig-rsrp','sig-rsrq','sig-sinr','sig-rssi','sig-bars','sig-network-mode','sig-nrrsrp','sig-nrrsrq','sig-nrsinr','sig-cell-id','sig-band','sig-bw','sig-plmn','sig-ant','sig-allowed'].forEach((id) => {{
      set(id, '—');
    }});
    const link = document.getElementById('sig-lteitaly');
    const badge = document.getElementById('networkBadge');
    if (badge) badge.hidden = true;
    if (link) {{
      link.setAttribute('href', '#');
      link.textContent = '—';
    }}
    if (payload && payload.error) {{
      const status = document.getElementById('status');
      status.textContent = 'Errore segnale: ' + payload.error;
      status.classList.add('err');
    }}
    renderSignalCharts();
    return;
  }}

  set('sig-rsrp', payload.rsrp);
  set('sig-rsrq', payload.rsrq);
  set('sig-sinr', payload.sinr);
  set('sig-rssi', payload.rssi || '—');
  set('sig-bars', payload.signal_icon ? fmtBars(payload.signal_icon) : (payload.signal_strength || '—'));
  set('sig-network-mode', payload.mode === '4G+' ? '4G+' : '4G');
  const badge = document.getElementById('networkBadge');
  if (badge) badge.hidden = false;
  set('sig-nrrsrp', payload.nrrsrp || '—');
  set('sig-nrrsrq', payload.nrrsrq || '—');
  set('sig-nrsinr', payload.nrsinr || '—');
  set('sig-cell-id', payload.cell_id || '—');
  set('sig-band', payload.band || '—');
  const bwText = (payload.dlbandwidth || '') + (payload.dlbandwidth || payload.ulbandwidth ? '/' : '') + (payload.ulbandwidth || '');
  set('sig-bw', bwText.replace(new RegExp('^/|/$', 'g'), '') || '—');
  set('sig-plmn', payload.plmn || '—');
  set('sig-ant', (payload.antenna1 || '--') + '/' + (payload.antenna2 || '--'));
  set('sig-allowed', payload.allowed || '—');

  const link = document.getElementById('sig-lteitaly');
  if (link) {{
    if (payload.lteitaly && payload.enbid) {{
      link.setAttribute('href', payload.lteitaly);
      link.textContent = String(payload.enbid);
    }} else {{
      link.setAttribute('href', '#');
      link.textContent = payload.enbid ? String(payload.enbid) : '—';
    }}
  }}
  const status = document.getElementById('status');
  if (status) status.classList.remove('err');
}}

function addPoint(down, up) {{
  history.push({{ down, up }});
  if (history.length > maxPoints) history.shift();
  renderChart();
}}

function renderChart() {{
  const svg = document.getElementById('chart');
  const w = svg.clientWidth || 520;
  const h = svg.clientHeight || 220;
  const pad = 28;
  const innerW = Math.max(1, w - pad * 2);
  const innerH = Math.max(1, h - pad * 2);
  const maxVal = Math.max(1, 10, ...history.map(p => Math.max(p.down, p.up)));
  const len = history.length || 1;

  const points = (key) => history.map((p, i) => {{
    const x = pad + (len <= 1 ? 0 : (i / (len - 1)) * innerW);
    const y = pad + innerH * (1 - (p[key] / maxVal));
    return x + ',' + y;
  }}).join(' ');

  const grid = [];
  for (let i = 0; i <= 4; i++) {{
    const y = pad + (innerH / 4) * i;
    grid.push('<line x1="' + pad + '" y1="' + y + '" x2="' + (pad + innerW) + '" y2="' + y + '" class="grid"/>');
    const label = (maxVal - (maxVal / 4) * i).toFixed(0);
    grid.push('<text x="' + (pad - 8) + '" y="' + (y + 4) + '" class="label">' + label + '</text>');
  }}

  svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
  svg.innerHTML =
    '<defs>' +
      '<linearGradient id="gradDl" x1="0%" y1="0%" x2="100%" y2="0%">' +
        '<stop offset="0%" stop-color="#60a5fa"/>' +
        '<stop offset="100%" stop-color="#2563eb"/>' +
      '</linearGradient>' +
      '<linearGradient id="gradUl" x1="0%" y1="0%" x2="100%" y2="0%">' +
        '<stop offset="0%" stop-color="#34d399"/>' +
        '<stop offset="100%" stop-color="#0ea66c"/>' +
      '</linearGradient>' +
    '</defs>' +
    '<rect x="0" y="0" width="' + w + '" height="' + h + '" rx="14" class="chart-bg"/>' +
    grid.join('') +
    (history.length ? '<polyline points="' + points('down') + '" class="line dl"/>' : '') +
    (history.length ? '<polyline points="' + points('up') + '" class="line ul"/>' : '');
}}

async function fetchJsonWithTimeout(url, headers) {{
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), Math.max(3500, pollMs * 2));
  try {{
    const response = await fetch(url, {{ cache: 'no-store', signal: controller.signal, headers: headers || {{}} }});
    return await response.json();
  }} finally {{
    clearTimeout(timeout);
  }}
}}

async function tick() {{
  const status = document.getElementById('status');
  const foot = document.getElementById('errorFoot');

  const requests = [fetchJsonWithTimeout('/api/speed')];
  if (radioEnabled) {{
    requests.push(fetchJsonWithTimeout('/api/signal', signalHeaders()));
  }}
  const results = await Promise.allSettled(requests);
  const speedResult = results[0];
  const signalResult = radioEnabled ? results[1] : null;

  let errors = [];

  if (speedResult.status === 'fulfilled') {{
    const j = speedResult.value;
    if (j.error) {{
      document.getElementById('down').textContent = '—';
      document.getElementById('up').textContent = '—';
      errors.push('speed: ' + j.error);
    }} else {{
      const down = num(j.download_mbps);
      const up = num(j.upload_mbps);
      document.getElementById('down').textContent = fmt(down) + ' Mbps';
      document.getElementById('up').textContent = fmt(up) + ' Mbps';
      addPoint(down, up);
    }}
  }} else {{
    document.getElementById('down').textContent = '—';
    document.getElementById('up').textContent = '—';
    errors.push('speed: ' + speedResult.reason);
  }}

  if (!radioEnabled) {{
    updateRouterLoginState('Dati radio disattivati.');
  }} else if (signalResult.status === 'fulfilled') {{
    const payload = signalResult.value;
    renderSignal(payload);
    addSignalPoint(payload);
    if (payload.error) {{
      errors.push('signal: ' + payload.error);
      if (isRouterPasswordError(payload.error)) {{
        stopRadioForPasswordError(payload.error);
      }} else if (String(payload.error).includes('100003')) {{
        updateRouterLoginState('Password richiesta dal router per i dati radio.');
      }}
    }} else {{
      updateRouterLoginState(routerPassword ? 'Dati radio autenticati.' : 'Dati radio disponibili senza password.');
    }}
  }} else {{
    renderSignal({{ error: String(signalResult.reason) }});
    errors.push('signal: ' + signalResult.reason);
  }}

  if (errors.length) {{
    status.textContent = 'Errore router';
    status.classList.add('err');
    foot.textContent = errors.join(' | ');
    foot.classList.remove('hide');
  }} else {{
    status.textContent = 'Ultimo aggiornamento: ' + new Date().toLocaleTimeString();
    status.classList.remove('err');
    foot.textContent = '';
    foot.classList.add('hide');
  }}
}}

async function loop() {{
  if (paused) return;
  await tick();
  if (paused) return;
  timer = setTimeout(loop, pollMs); // aspetta che la richiesta finisca per evitare sovrapposizioni
}}

loadRouterPassword();
loadRadioPreferences();
renderChart();
loop();

function pausePolling(reason) {{
  paused = true;
  if (timer) {{
    clearTimeout(timer);
    timer = null;
  }}
  const status = document.getElementById('status');
  status.textContent = 'In pausa: ' + reason;
  status.classList.add('err');
  const foot = document.getElementById('errorFoot');
  if (foot) {{
    foot.textContent = 'Polling fermo (' + reason + ')';
    foot.classList.remove('hide');
  }}
}}

function resumePolling() {{
  if (!paused) return;
  paused = false;
  const status = document.getElementById('status');
  status.textContent = 'Riprendo…';
  status.classList.remove('err');
  const foot = document.getElementById('errorFoot');
  if (foot) {{
    foot.textContent = '';
    foot.classList.add('hide');
  }}
  loop();
}}

document.addEventListener('visibilitychange', () => {{
  if (document.visibilityState === 'hidden') {{
    pausePolling('scheda non visibile');
  }} else {{
    resumePolling();
  }}
}});

setupMetricModal();
renderSignalCharts();
updateInstallButton();
</script>
</body>
</html>
""")
