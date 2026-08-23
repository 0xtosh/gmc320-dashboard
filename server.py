"""
Standalone web server for the GMC-320 dashboard. Stdlib-only (no Flask/etc.)
so it drops straight onto a Raspberry Pi with nothing but `pip install pyserial`.

Usage:
    python server.py                       # auto-detect serial port
    python server.py --port /dev/ttyUSB0   # explicit port (typical on a Pi)
    python server.py --http-port 8080

Endpoints:
    GET  /                       dashboard HTML (live panel + history chart)
    GET  /api/live                {cpm, voltage, version, age_seconds}
    POST /api/refresh             kick off a full history download (background)
    GET  /api/refresh/status      progress of an in-flight/last download
    GET  /data/history.csv        raw parsed history (for external analysis)
    GET  /data/history_raw.bin    raw flash dump (debug)
"""
import argparse
import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import dashboard
import download_history
from gqgmc import GMC320, GMCConfig, GMCError, find_gmc_port

DATA_DIR = Path(__file__).parent / "data"

serial_lock = threading.Lock()

live_state = {"cpm": None, "voltage": None, "version": None, "updated_at": None}
refresh_state = {
    "running": False,
    "done_bytes": 0,
    "total_bytes": 0,
    "error": None,
    "last_completed": None,
    "just_finished": False,
}

SERIAL_PORT = None  # set in main()


def live_poll_loop(poll_interval: float):
    while True:
        if serial_lock.acquire(blocking=False):
            try:
                cfg = GMCConfig(port=SERIAL_PORT, baud=19200, settle=0.3)
                with GMC320(cfg) as dev:
                    cpm = dev.get_cpm()
                    volt = dev.get_voltage()
                    ver = live_state["version"] or dev.get_version()
                live_state.update(cpm=cpm, voltage=volt, version=ver, updated_at=time.time())
            except Exception:
                pass  # device may be mid-refresh or momentarily unavailable
            finally:
                serial_lock.release()
        time.sleep(poll_interval)


def do_refresh():
    refresh_state.update(running=True, done_bytes=0, total_bytes=0, error=None, just_finished=False)
    try:
        with serial_lock:

            def progress(done, total):
                refresh_state["done_bytes"] = done
                refresh_state["total_bytes"] = total

            download_history.run_download(SERIAL_PORT, progress=progress)
        refresh_state["last_completed"] = datetime.now().isoformat(timespec="seconds")
        refresh_state["just_finished"] = True
    except Exception as e:
        refresh_state["error"] = str(e)
    finally:
        refresh_state["running"] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep stdout quiet; default logging is noisy for a Pi console

    def _send(self, status, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status=200):
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _send_file(self, path: Path, content_type: str):
        if not path.exists():
            self._send_json({"error": "not found"}, status=404)
            return
        self._send(200, path.read_bytes(), content_type)

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path

        if path == "/":
            html = dashboard.render_html(dashboard.CSV_PATH, live_enabled=True)
            self._send(200, html.encode(), "text/html; charset=utf-8")
        elif path == "/api/history":
            qs = parse_qs(parsed.query)
            range_key = qs.get("range", [dashboard.DEFAULT_RANGE])[0]
            if range_key not in dashboard.RANGE_LOOKUP:
                range_key = dashboard.DEFAULT_RANGE
            payload = dashboard.get_range_payload(dashboard.CSV_PATH, range_key)
            self._send_json(payload)
        elif self.path == "/api/live":
            age = None if live_state["updated_at"] is None else time.time() - live_state["updated_at"]
            self._send_json(
                {
                    "cpm": live_state["cpm"],
                    "voltage": live_state["voltage"],
                    "version": live_state["version"],
                    "age_seconds": round(age, 1) if age is not None else None,
                }
            )
        elif self.path == "/api/refresh/status":
            self._send_json(refresh_state)
            refresh_state["just_finished"] = False  # one-shot flag, consumed on read
        elif self.path == "/data/history.csv":
            self._send_file(download_history.CSV_PATH, "text/csv")
        elif self.path == "/data/history_raw.bin":
            self._send_file(download_history.RAW_PATH, "application/octet-stream")
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        if self.path == "/api/refresh":
            if refresh_state["running"]:
                self._send_json({"status": "busy"})
            else:
                threading.Thread(target=do_refresh, daemon=True).start()
                self._send_json({"status": "started"})
        else:
            self._send_json({"error": "not found"}, status=404)


def main():
    global SERIAL_PORT
    ap = argparse.ArgumentParser(description="GMC-320 dashboard web server")
    ap.add_argument("--port", default=None, help="Serial port, e.g. COM6 or /dev/ttyUSB0 (default: auto-detect)")
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--poll-interval", type=float, default=5.0, help="Seconds between live CPM polls")
    args = ap.parse_args()

    try:
        SERIAL_PORT = args.port or find_gmc_port()
    except GMCError as e:
        raise SystemExit(f"{e}\nList available ports with: python -m serial.tools.list_ports")

    print(f"Using serial port: {SERIAL_PORT}")
    DATA_DIR.mkdir(exist_ok=True)

    threading.Thread(target=live_poll_loop, args=(args.poll_interval,), daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.http_port), Handler)
    print(f"Serving on http://{args.host}:{args.http_port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
