import os
import time
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from huawei_lte_api.Connection import Connection
from huawei_lte_api.api.Monitoring import Monitoring

ROUTER_HOST = os.getenv("ROUTER_HOST", "")
ROUTER_USER = os.getenv("ROUTER_USER", "")
ROUTER_PASSWORD = os.getenv("ROUTER_PASSWORD", "")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", ""))

app = FastAPI()

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
        with Connection(url) as conn:
            mon = Monitoring(conn)
            s = mon.traffic_statistics()
            return _extract_rx_tx(s), _extract_rx_tx(s, upload=True), s

    # Altrimenti usa login
    with Connection(url, username=ROUTER_USER or None, password=ROUTER_PASSWORD) as conn:
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

@app.get("/api/speed")
def api_speed():
    rx, tx, raw = _get_stats()
    _update_rates(rx, tx, _extract_rate(raw), _extract_rate(raw, upload=True))
    return JSONResponse({
        "router": ROUTER_HOST,
        "download_mbps": _human_mbps(_last["rx_rate_smooth"]),
        "upload_mbps": _human_mbps(_last["tx_rate_smooth"]),
        "poll_seconds": POLL_SECONDS,
        "raw": raw,  # utile per debug: puoi toglierlo dopo
    })

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Vodafone FWA Monitor</title>
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
      <div class="foot hide" id="errorFoot"></div>
    </div>
  </div>

<script>
const pollMs = {int(POLL_SECONDS*1000)};
const maxPoints = Math.max(10, Math.floor(180000 / pollMs)); // ~3 minuti di storico
const history = [];
let timer = null;
let paused = false;

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

const num = (n) => Number(n || 0);
const fmt = (n) => num(n).toFixed(2);

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

async function tick() {{
  try {{
    const r = await fetch('/api/speed', {{ cache: 'no-store' }});
    const j = await r.json();
    const down = num(j.download_mbps);
    const up = num(j.upload_mbps);

    document.getElementById('down').textContent = fmt(down) + ' Mbps';
    document.getElementById('up').textContent = fmt(up) + ' Mbps';
    const status = document.getElementById('status');
    status.textContent = 'Ultimo aggiornamento: ' + new Date().toLocaleTimeString();
    status.classList.remove('err');
    const foot = document.getElementById('errorFoot');
    foot.textContent = '';
    foot.classList.add('hide');
    addPoint(down, up);
  }} catch (e) {{
    const status = document.getElementById('status');
    status.textContent = 'Errore: ' + e;
    status.classList.add('err');
    const foot = document.getElementById('errorFoot');
    foot.textContent = 'Errore di polling: ' + e;
    foot.classList.remove('hide');
  }}
}}

async function loop() {{
  if (paused) return;
  await tick();
  if (paused) return;
  timer = setTimeout(loop, pollMs); // aspetta che la richiesta finisca per evitare sovrapposizioni
}}

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
</script>
</body>
</html>
""")
