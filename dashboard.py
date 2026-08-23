"""
Shared dashboard rendering: turns data/history.csv into a self-contained HTML
page. Used both by build_dashboard.py (offline/static file) and server.py
(served live, with a "current reading" panel and a server-backed time-range
selector via /api/history).
"""
import bisect
import csv
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
CSV_PATH = DATA_DIR / "history.csv"

TARGET_BUCKETS = 700

# (key, label, lookback) -- lookback=None means "all time"
RANGE_OPTIONS = [
    ("1h", "Last hour", timedelta(hours=1)),
    ("6h", "Last 6 hours", timedelta(hours=6)),
    ("1d", "Last 24 hours", timedelta(days=1)),
    ("7d", "Last 7 days", timedelta(days=7)),
    ("30d", "Last 30 days", timedelta(days=30)),
    ("90d", "Last 90 days", timedelta(days=90)),
    ("all", "All time", None),
]
RANGE_LOOKUP = {key: (label, delta) for key, label, delta in RANGE_OPTIONS}
DEFAULT_RANGE = "all"


def load_rows(csv_path: Path = CSV_PATH):
    rows = []
    if not csv_path.exists():
        return rows
    with csv_path.open(newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if not row["timestamp"]:
                continue
            rows.append((datetime.fromisoformat(row["timestamp"]), int(row["cpm"])))
    rows.sort(key=lambda x: x[0])
    return rows


# Cache the (potentially ~1M row) CSV in memory, keyed by the file's mtime so
# a history refresh naturally invalidates it without an explicit signal.
_cache = {"mtime": None, "rows": [], "times": []}


def _load_cached(csv_path: Path):
    try:
        mtime = csv_path.stat().st_mtime
    except FileNotFoundError:
        return [], []
    if _cache["mtime"] != mtime:
        rows = load_rows(csv_path)
        _cache["rows"] = rows
        _cache["times"] = [r[0] for r in rows]
        _cache["mtime"] = mtime
    return _cache["rows"], _cache["times"]


def filter_range(rows, times, range_key: str):
    if not rows:
        return rows
    _, delta = RANGE_LOOKUP.get(range_key, (None, None))
    if delta is None:
        return rows
    cutoff = rows[-1][0] - delta
    idx = bisect.bisect_left(times, cutoff)
    return rows[idx:]


def bucketize(rows, target_buckets=TARGET_BUCKETS):
    if not rows:
        return []
    t0 = rows[0][0].timestamp()
    t1 = rows[-1][0].timestamp()
    span = max(t1 - t0, 1)
    bucket_size = max(span / target_buckets, 1)

    buckets = {}
    for ts, cpm in rows:
        idx = int((ts.timestamp() - t0) // bucket_size)
        buckets.setdefault(idx, []).append(cpm)

    out = []
    for idx in sorted(buckets):
        vals = buckets[idx]
        center_t = t0 + (idx + 0.5) * bucket_size
        out.append(
            {
                "t": datetime.fromtimestamp(center_t).isoformat(),
                "avg": round(sum(vals) / len(vals), 2),
                "min": min(vals),
                "max": max(vals),
            }
        )
    return out


def compute_stats(rows):
    if not rows:
        return {}
    vals = [c for _, c in rows]
    n = len(vals)
    mean = sum(vals) / n
    variance = sum((v - mean) ** 2 for v in vals) / n
    return {
        "count": n,
        "start": rows[0][0].isoformat(),
        "end": rows[-1][0].isoformat(),
        "duration_hours": round((rows[-1][0] - rows[0][0]).total_seconds() / 3600, 2),
        "avg_cpm": round(mean, 2),
        "min_cpm": min(vals),
        "max_cpm": max(vals),
        "stdev_cpm": round(math.sqrt(variance), 2),
    }


def format_duration(hours) -> str:
    if hours is None:
        return "-"
    if hours < 48:
        return f"{hours:g} h"
    days = hours / 24
    if days < 14:
        return f"{days:.1f} d"
    weeks = days / 7
    return f"{weeks:.1f} wk"


def get_range_payload(csv_path: Path, range_key: str, target_buckets: int = TARGET_BUCKETS) -> dict:
    rows, times = _load_cached(csv_path)
    filtered = filter_range(rows, times, range_key)
    stats = compute_stats(filtered)
    buckets = bucketize(filtered, target_buckets)
    return {"buckets": buckets, "stats": stats, "range": range_key}


HTML_TEMPLATE = """<title>GMC-320 Radiation Dashboard</title>
<style>
html, body {{ margin: 0; padding: 0; height: 100%; }}
.viz-root {{
  color-scheme: light;
  --surface-1:      #fcfcfb;
  --surface-2:      #f3f2ef;
  --page:           #f2f1ee;
  --text-primary:   #0b0b0b;
  --text-secondary: #52514e;
  --text-muted:     #898781;
  --grid:           #e7e6e1;
  --baseline:       #c3c2b7;
  --series-1:       #2a78d6;
  --series-1-strong:#1c5cab;
  --band-fill:      rgba(42,120,214,0.10);
  --border:         rgba(11,11,11,0.09);
  --shadow:         0 1px 2px rgba(11,11,11,0.04), 0 4px 16px rgba(11,11,11,0.05);
  --good:           #0ca30c;
  --critical:       #d03b3b;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) .viz-root {{
    color-scheme: dark;
    --surface-1:      #171716;
    --surface-2:      #1f1f1d;
    --page:           #0d0d0d;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #898781;
    --grid:           #292927;
    --baseline:       #383835;
    --series-1:       #3987e5;
    --series-1-strong:#86b6ef;
    --band-fill:      rgba(57,135,229,0.14);
    --border:         rgba(255,255,255,0.09);
    --shadow:         0 1px 2px rgba(0,0,0,0.3), 0 4px 20px rgba(0,0,0,0.35);
    --good:           #0ca30c;
    --critical:       #e66767;
  }}
}}
:root[data-theme="dark"] .viz-root {{
  color-scheme: dark;
  --surface-1:      #171716;
  --surface-2:      #1f1f1d;
  --page:           #0d0d0d;
  --text-primary:   #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted:     #898781;
  --grid:           #292927;
  --baseline:       #383835;
  --series-1:       #3987e5;
  --series-1-strong:#86b6ef;
  --band-fill:      rgba(57,135,229,0.14);
  --border:         rgba(255,255,255,0.09);
  --shadow:         0 1px 2px rgba(0,0,0,0.3), 0 4px 20px rgba(0,0,0,0.35);
  --good:           #0ca30c;
  --critical:       #e66767;
}}

.viz-root {{
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--page);
  color: var(--text-primary);
  box-sizing: border-box;
  height: 100vh;
}}
.viz-root * {{ box-sizing: border-box; }}
.viz-root .page {{
  max-width: 1400px;
  height: 100%;
  margin: 0 auto;
  padding: 14px 22px 14px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow-y: auto;
}}

.appbar {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  flex: 0 0 auto;
  padding-bottom: 10px;
  margin-bottom: 10px;
  border-bottom: 1px solid var(--border);
}}
.appbar-title {{ display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }}
h1 {{ font-size: 17px; font-weight: 700; margin: 0; letter-spacing: -0.01em; }}
.subtitle {{ color: var(--text-secondary); font-size: 12px; margin: 0; }}
.badge {{
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
  padding: 3px 8px; border-radius: 100px;
  background: var(--surface-2); color: var(--text-secondary); border: 1px solid var(--border);
}}
.badge .dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--good); }}
.badge .dot.stale {{ background: var(--text-muted); }}

.toolbar {{
  display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap;
  flex: 0 0 auto;
}}
select, .btn {{
  font-family: inherit;
  font-size: 13px; font-weight: 600;
  border-radius: 8px;
  padding: 8px 14px;
  cursor: pointer;
}}
select {{
  background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--border);
  appearance: none;
  -webkit-appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23898781'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 12px center;
  padding-right: 30px;
}}
select:focus, .btn:focus-visible {{ outline: 2px solid var(--series-1); outline-offset: 1px; }}
.btn {{ background: var(--series-1); color: white; border: none; box-shadow: var(--shadow); }}
.btn:hover {{ background: var(--series-1-strong); }}
.btn:disabled {{ opacity: 0.5; cursor: default; background: var(--series-1); }}
.btn.secondary {{ background: transparent; color: var(--text-primary); border: 1px solid var(--border); box-shadow: none; }}
.btn.secondary:hover {{ background: var(--surface-2); }}
.refresh-status {{ font-size: 12px; color: var(--text-secondary); }}
.spacer {{ flex: 1; }}

.live-row {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
  margin-bottom: 8px;
  flex: 0 0 auto;
}}
.stats {{
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 8px;
  margin-bottom: 10px;
  flex: 0 0 auto;
}}
@media (max-width: 900px) {{
  .stats {{ grid-template-columns: repeat(3, 1fr); }}
}}
@media (max-width: 520px) {{
  .stats {{ grid-template-columns: repeat(2, 1fr); }}
}}
.stat-tile {{
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 9px 12px;
  box-shadow: var(--shadow);
  min-width: 0;
}}
.stat-tile.live {{ box-shadow: var(--shadow), inset 0 0 0 1px var(--series-1); position: relative; }}
.stat-label {{
  font-size: 10.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em;
  color: var(--text-muted); margin-bottom: 3px; display: flex; align-items: center; gap: 6px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.dot {{ width: 7px; height: 7px; border-radius: 50%; background: var(--good); display: inline-block; flex-shrink: 0; }}
.dot.stale {{ background: var(--text-muted); }}
.stat-value {{
  font-size: 18px; font-weight: 650; color: var(--text-primary);
  font-variant-numeric: tabular-nums; letter-spacing: -0.01em;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.stat-unit {{ font-size: 11px; font-weight: 500; color: var(--text-secondary); margin-left: 3px; }}

.chart-card {{
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 18px 12px;
  box-shadow: var(--shadow);
  position: relative;
  flex: 1 1 auto;
  min-height: 200px;
  display: flex;
  flex-direction: column;
}}
.chart-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; flex-wrap: wrap; flex: 0 0 auto; }}
.chart-title {{ font-size: 13px; font-weight: 650; margin: 0; }}
.legend {{ display: flex; align-items: center; gap: 14px; font-size: 11.5px; color: var(--text-secondary); }}
.legend-item {{ display: flex; align-items: center; gap: 6px; }}
.legend-swatch {{ width: 14px; height: 3px; border-radius: 2px; background: var(--series-1); }}
.legend-swatch.band {{ background: var(--band-fill); height: 10px; border-radius: 3px; }}

#chart-container {{ position: relative; flex: 1 1 auto; min-height: 0; }}
svg {{
  display: block;
  width: 100%;
  height: 100%;
  overflow: visible;
}}
.gridline {{ stroke: var(--grid); stroke-width: 1; }}
.baseline {{ stroke: var(--baseline); stroke-width: 1; }}
.axis-label {{ fill: var(--text-muted); font-size: 11px; }}
.band {{ fill: var(--band-fill); }}
.line {{ fill: none; stroke: var(--series-1); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }}

.tooltip {{
  position: absolute;
  pointer-events: none;
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 12px;
  color: var(--text-primary);
  box-shadow: 0 4px 16px rgba(0,0,0,0.15);
  opacity: 0;
  transition: opacity 0.1s;
  white-space: nowrap;
  z-index: 2;
}}
.crosshair {{ stroke: var(--text-muted); stroke-width: 1; stroke-dasharray: 3 3; opacity: 0; }}
.hover-dot {{ fill: var(--series-1); stroke: var(--surface-1); stroke-width: 2; opacity: 0; }}
.empty {{
  color: var(--text-muted); font-size: 13px; text-align: center;
  height: 100%; display: flex; align-items: center; justify-content: center;
}}
.chart-card.loading svg {{ opacity: 0.35; transition: opacity 0.15s; }}

footer.foot {{
  flex: 0 0 auto; margin-top: 8px; font-size: 11px; color: var(--text-muted); text-align: center;
}}
</style>

<div class="viz-root">
<div class="page">
  <div class="appbar">
    <div class="appbar-title">
      <h1>GMC-320 Radiation Dashboard</h1>
      {status_badge}
    </div>
    <p class="subtitle" id="subtitle">{subtitle}</p>
  </div>

  {toolbar}

  <div class="live-row" id="live-row" style="display:none;">
    <div class="stat-tile live">
      <div class="stat-label"><span class="dot" id="live-dot"></span>Live CPM</div>
      <div class="stat-value" id="live-cpm">-</div>
    </div>
    <div class="stat-tile live">
      <div class="stat-label">Battery</div>
      <div class="stat-value" id="live-volt">-<span class="stat-unit">V</span></div>
    </div>
    <div class="stat-tile live">
      <div class="stat-label">Device</div>
      <div class="stat-value" id="live-ver" style="font-size:14px;">-</div>
    </div>
  </div>

  <div class="stats" id="stats">
    <div class="stat-tile"><div class="stat-label">Average</div><div class="stat-value" id="s-avg">{avg_cpm}<span class="stat-unit">CPM</span></div></div>
    <div class="stat-tile"><div class="stat-label">Peak</div><div class="stat-value" id="s-max">{max_cpm}<span class="stat-unit">CPM</span></div></div>
    <div class="stat-tile"><div class="stat-label">Minimum</div><div class="stat-value" id="s-min">{min_cpm}<span class="stat-unit">CPM</span></div></div>
    <div class="stat-tile"><div class="stat-label">Std dev</div><div class="stat-value" id="s-stdev">{stdev_cpm}<span class="stat-unit">CPM</span></div></div>
    <div class="stat-tile"><div class="stat-label">Span</div><div class="stat-value" id="s-duration">{duration_label}</div></div>
    <div class="stat-tile"><div class="stat-label">Samples</div><div class="stat-value" id="s-count">{count}</div></div>
  </div>

  <div class="chart-card" id="chart-card">
    <div class="chart-head">
      <p class="chart-title">Radiation level over time</p>
      <div class="legend">
        <span class="legend-item"><span class="legend-swatch"></span>Average</span>
        <span class="legend-item"><span class="legend-swatch band"></span>Min&ndash;max range</span>
      </div>
    </div>
    <div id="chart-container">
      {chart_body}
    </div>
  </div>

  <footer class="foot">GQ GMC-320 &middot; served locally from your device</footer>
</div>
</div>

<script>
const INITIAL_BUCKETS = {buckets_json};
const LIVE_ENABLED = {live_enabled_json};
const RANGE_OPTIONS = {range_options_json};

function formatDuration(hours) {{
  if (hours === null || hours === undefined) return '-';
  if (hours < 48) {{
    const r = Math.round(hours * 10) / 10;
    return (r % 1 === 0 ? r.toFixed(0) : r.toFixed(1)) + ' h';
  }}
  const days = hours / 24;
  if (days < 14) return days.toFixed(1) + ' d';
  return (days / 7).toFixed(1) + ' wk';
}}

function updateStats(stats) {{
  const set = (id, html) => {{ const e = document.getElementById(id); if (e) e.innerHTML = html; }};
  if (!stats || !stats.count) {{
    set('s-avg', '-'); set('s-max', '-'); set('s-min', '-'); set('s-stdev', '-');
    set('s-duration', '-'); set('s-count', '0');
    return;
  }}
  set('s-avg', stats.avg_cpm + '<span class="stat-unit">CPM</span>');
  set('s-max', stats.max_cpm + '<span class="stat-unit">CPM</span>');
  set('s-min', stats.min_cpm + '<span class="stat-unit">CPM</span>');
  set('s-stdev', stats.stdev_cpm + '<span class="stat-unit">CPM</span>');
  set('s-duration', formatDuration(stats.duration_hours));
  set('s-count', stats.count.toLocaleString());
  const sub = document.getElementById('subtitle');
  if (sub) sub.textContent = stats.count.toLocaleString() + ' samples, ' +
    new Date(stats.start).toLocaleString() + ' \\u2192 ' + new Date(stats.end).toLocaleString();
}}

const ns = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs) {{
  const e = document.createElementNS(ns, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}}

let currentBuckets = [];

function renderChart(buckets) {{
  currentBuckets = buckets;
  const container = document.getElementById('chart-container');
  if (!buckets || buckets.length === 0) {{
    container.innerHTML = '<div class="empty">No data in this range yet.</div>';
    return;
  }}
  // Size the viewBox to the container's ACTUAL pixel size (1 unit = 1px) so
  // nothing gets non-uniformly stretched -- a fixed viewBox with
  // preserveAspectRatio="none" on a variable-height flex container distorts
  // text and strokes whenever the rendered aspect ratio drifts from the
  // viewBox's own aspect ratio.
  const box = container.getBoundingClientRect();
  const W = Math.max(Math.round(box.width), 100);
  const H = Math.max(Math.round(box.height), 100);
  container.innerHTML = '<svg id="chart" viewBox="0 0 ' + W + ' ' + H + '"></svg><div class="tooltip" id="tooltip"></div>';
  const svg = document.getElementById('chart');
  const tooltip = document.getElementById('tooltip');
  const PAD_L = 46, PAD_R = 12, PAD_T = 12, PAD_B = 30;
  const plotW = W - PAD_L - PAD_R, plotH = H - PAD_T - PAD_B;

  const times = buckets.map(b => new Date(b.t).getTime());
  const maxes = buckets.map(b => b.max);
  const mins = buckets.map(b => b.min);
  const avgs = buckets.map(b => b.avg);
  const tMin = Math.min(...times), tMax = Math.max(...times);
  const vMax = Math.max(...maxes) * 1.08 || 1;
  const vMin = 0;

  const x = t => PAD_L + (t - tMin) / (tMax - tMin || 1) * plotW;
  const y = v => PAD_T + plotH - (v - vMin) / (vMax - vMin || 1) * plotH;

  const yTicks = 5;
  for (let i = 0; i <= yTicks; i++) {{
    const v = vMin + (vMax - vMin) * i / yTicks;
    const yy = y(v);
    svg.appendChild(svgEl('line', {{x1: PAD_L, x2: W - PAD_R, y1: yy, y2: yy, class: 'gridline'}}));
    const label = svgEl('text', {{x: PAD_L - 8, y: yy + 4, class: 'axis-label', 'text-anchor': 'end'}});
    label.textContent = Math.round(v);
    svg.appendChild(label);
  }}
  svg.appendChild(svgEl('line', {{x1: PAD_L, x2: W - PAD_R, y1: PAD_T + plotH, y2: PAD_T + plotH, class: 'baseline'}}));

  const xTicks = 5;
  for (let i = 0; i <= xTicks; i++) {{
    const t = tMin + (tMax - tMin) * i / xTicks;
    const xx = x(t);
    const d = new Date(t);
    const label = svgEl('text', {{x: xx, y: H - 8, class: 'axis-label', 'text-anchor': i === 0 ? 'start' : (i === xTicks ? 'end' : 'middle')}});
    label.textContent = d.toLocaleString(undefined, {{month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}});
    svg.appendChild(label);
  }}

  let bandPath = 'M ' + x(times[0]) + ' ' + y(mins[0]);
  for (let i = 1; i < times.length; i++) bandPath += ' L ' + x(times[i]) + ' ' + y(mins[i]);
  for (let i = times.length - 1; i >= 0; i--) bandPath += ' L ' + x(times[i]) + ' ' + y(maxes[i]);
  bandPath += ' Z';
  svg.appendChild(svgEl('path', {{d: bandPath, class: 'band'}}));

  let linePath = 'M ' + x(times[0]) + ' ' + y(avgs[0]);
  for (let i = 1; i < times.length; i++) linePath += ' L ' + x(times[i]) + ' ' + y(avgs[i]);
  svg.appendChild(svgEl('path', {{d: linePath, class: 'line'}}));

  const crosshair = svgEl('line', {{class: 'crosshair', y1: PAD_T, y2: PAD_T + plotH}});
  svg.appendChild(crosshair);
  const dot = svgEl('circle', {{class: 'hover-dot', r: 4}});
  svg.appendChild(dot);
  const hitRect = svgEl('rect', {{x: PAD_L, y: PAD_T, width: plotW, height: plotH, fill: 'transparent'}});
  svg.appendChild(hitRect);

  function nearestIdx(px) {{
    const t = tMin + (px - PAD_L) / plotW * (tMax - tMin);
    let lo = 0, hi = times.length - 1;
    while (lo < hi) {{
      const mid = (lo + hi) >> 1;
      if (times[mid] < t) lo = mid + 1; else hi = mid;
    }}
    return lo;
  }}

  hitRect.addEventListener('mousemove', function(ev) {{
    const rect = svg.getBoundingClientRect();
    const scaleX = W / rect.width;
    const px = (ev.clientX - rect.left) * scaleX;
    const idx = nearestIdx(px);
    const b = buckets[idx];
    const xx = x(times[idx]);
    crosshair.setAttribute('x1', xx);
    crosshair.setAttribute('x2', xx);
    crosshair.setAttribute('opacity', 1);
    dot.setAttribute('cx', xx);
    dot.setAttribute('cy', y(b.avg));
    dot.setAttribute('opacity', 1);
    const d = new Date(times[idx]);
    tooltip.innerHTML = '<strong>' + d.toLocaleString() + '</strong><br>avg ' + b.avg + ' CPM (range ' + b.min + '\\u2013' + b.max + ')';
    const cRect = container.getBoundingClientRect();
    let left = (ev.clientX - cRect.left) + 12;
    if (left + 160 > cRect.width) left = (ev.clientX - cRect.left) - 172;
    tooltip.style.left = left + 'px';
    tooltip.style.top = ((ev.clientY - cRect.top) - 36) + 'px';
    tooltip.style.opacity = 1;
  }});
  hitRect.addEventListener('mouseleave', function() {{
    crosshair.setAttribute('opacity', 0);
    dot.setAttribute('opacity', 0);
    tooltip.style.opacity = 0;
  }});
}}

// Reveal the live-readings row (if any) BEFORE measuring the chart container --
// it changes the available height, and measuring too early leaves the chart's
// viewBox sized for space that then shrinks, distorting the redraw.
if (LIVE_ENABLED) {{
  document.getElementById('live-row').style.display = '';
}}
renderChart(INITIAL_BUCKETS);

let resizeTimer = null;
window.addEventListener('resize', function() {{
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(function() {{ renderChart(currentBuckets); }}, 120);
}});

function filterBucketsClientSide(buckets, rangeKey) {{
  const opt = RANGE_OPTIONS.find(o => o[0] === rangeKey);
  if (!opt || opt[2] === null || buckets.length === 0) return buckets;
  const lastT = new Date(buckets[buckets.length - 1].t).getTime();
  const cutoff = lastT - opt[2] * 1000;
  return buckets.filter(b => new Date(b.t).getTime() >= cutoff);
}}

function approxStatsFromBuckets(buckets) {{
  if (!buckets.length) return {{count: 0}};
  const avgs = buckets.map(b => b.avg);
  const mean = avgs.reduce((a, b) => a + b, 0) / avgs.length;
  const variance = avgs.reduce((a, b) => a + (b - mean) ** 2, 0) / avgs.length;
  return {{
    count: buckets.length,
    start: buckets[0].t,
    end: buckets[buckets.length - 1].t,
    duration_hours: (new Date(buckets[buckets.length - 1].t) - new Date(buckets[0].t)) / 3600000,
    avg_cpm: Math.round(mean * 100) / 100,
    min_cpm: Math.min(...buckets.map(b => b.min)),
    max_cpm: Math.max(...buckets.map(b => b.max)),
    stdev_cpm: Math.round(Math.sqrt(variance) * 100) / 100,
  }};
}}

const rangeSelect = document.getElementById('range-select');
const chartCard = document.getElementById('chart-card');
if (rangeSelect) {{
  rangeSelect.addEventListener('change', async function() {{
    const key = rangeSelect.value;
    chartCard.classList.add('loading');
    try {{
      if (LIVE_ENABLED) {{
        const res = await fetch('/api/history?range=' + encodeURIComponent(key));
        const data = await res.json();
        renderChart(data.buckets);
        updateStats(data.stats);
      }} else {{
        const filtered = filterBucketsClientSide(INITIAL_BUCKETS, key);
        renderChart(filtered);
        updateStats(approxStatsFromBuckets(filtered));
      }}
    }} finally {{
      chartCard.classList.remove('loading');
    }}
  }});
}}

if (LIVE_ENABLED) {{
  document.getElementById('live-row').style.display = '';

  async function pollLive() {{
    try {{
      const res = await fetch('/api/live');
      const data = await res.json();
      document.getElementById('live-cpm').textContent = data.cpm ?? '-';
      document.getElementById('live-volt').innerHTML = (data.voltage ?? '-') + '<span class="stat-unit">V</span>';
      document.getElementById('live-ver').textContent = data.version ?? '-';
      const dot = document.getElementById('live-dot');
      const age = data.age_seconds;
      dot.classList.toggle('stale', age === null || age > 30);
      const badgeDot = document.getElementById('badge-dot');
      if (badgeDot) badgeDot.classList.toggle('stale', age === null || age > 30);
    }} catch (e) {{ /* device may be busy mid-refresh; ignore */ }}
  }}
  pollLive();
  setInterval(pollLive, 5000);

  const btn = document.getElementById('refresh-btn');
  const statusEl = document.getElementById('refresh-status');
  if (btn) {{
    btn.addEventListener('click', async function() {{
      btn.disabled = true;
      await fetch('/api/refresh', {{method: 'POST'}});
      pollRefreshStatus();
    }});
  }}

  async function pollRefreshStatus() {{
    const res = await fetch('/api/refresh/status');
    const s = await res.json();
    if (s.running) {{
      const pct = s.total_bytes ? (100 * s.done_bytes / s.total_bytes).toFixed(1) : '0.0';
      statusEl.textContent = 'Downloading from device\\u2026 ' + pct + '%';
      if (btn) btn.disabled = true;
      setTimeout(pollRefreshStatus, 2000);
    }} else {{
      if (btn) btn.disabled = false;
      if (s.error) {{
        statusEl.textContent = 'Last refresh failed: ' + s.error;
      }} else if (s.last_completed) {{
        statusEl.textContent = 'Last refreshed ' + s.last_completed;
        if (s.just_finished) location.reload();
      }} else {{
        statusEl.textContent = '';
      }}
    }}
  }}
  pollRefreshStatus();
}}
</script>
"""


def render_html(csv_path: Path = CSV_PATH, live_enabled: bool = False) -> str:
    payload = get_range_payload(csv_path, DEFAULT_RANGE)
    stats = payload["stats"]
    buckets = payload["buckets"]

    if stats:
        def _fmt(iso):
            d = datetime.fromisoformat(iso)
            return f"{d:%b} {d.day}, {d:%Y %H:%M}"

        subtitle = f"{stats['count']:,} samples, {_fmt(stats['start'])} → {_fmt(stats['end'])}"
        chart_body = '<svg id="chart" viewBox="0 0 1200 420" preserveAspectRatio="none"></svg><div class="tooltip" id="tooltip"></div>'
    else:
        subtitle = "No history downloaded yet."
        chart_body = '<div class="empty">No data yet &mdash; click "Refresh from device" to download history.</div>'

    range_selector = "".join(
        f'<option value="{key}"{" selected" if key == DEFAULT_RANGE else ""}>{label}</option>'
        for key, label, _ in RANGE_OPTIONS
    )

    toolbar_parts = [f'<select id="range-select" aria-label="Time range">{range_selector}</select>']
    if live_enabled:
        toolbar_parts.append('<button class="btn" id="refresh-btn">Refresh from device</button>')
    toolbar_parts.append('<a class="btn secondary" href="/data/history.csv">Download CSV</a>')
    toolbar_parts.append('<span class="spacer"></span>')
    if live_enabled:
        toolbar_parts.append('<span class="refresh-status" id="refresh-status"></span>')
    toolbar = '<div class="toolbar">' + "".join(toolbar_parts) + "</div>"

    status_badge = (
        '<span class="badge"><span class="dot" id="badge-dot"></span>Live</span>'
        if live_enabled
        else '<span class="badge">Static export</span>'
    )

    return HTML_TEMPLATE.format(
        subtitle=subtitle,
        toolbar=toolbar,
        status_badge=status_badge,
        chart_body=chart_body,
        avg_cpm=stats.get("avg_cpm", "-"),
        max_cpm=stats.get("max_cpm", "-"),
        min_cpm=stats.get("min_cpm", "-"),
        stdev_cpm=stats.get("stdev_cpm", "-"),
        duration_label=format_duration(stats.get("duration_hours")),
        count=f"{stats.get('count', 0):,}",
        buckets_json=json.dumps(buckets),
        live_enabled_json=json.dumps(live_enabled),
        range_options_json=json.dumps(
            [[key, label, (delta.total_seconds() if delta else None)] for key, label, delta in RANGE_OPTIONS]
        ),
    )
