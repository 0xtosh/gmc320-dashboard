"""Download the full history flash from a GMC-320 and parse it into CSV + SQLite.

Importable (`run_download(port, progress=...)`) so server.py can drive the same
logic from a background thread; also runnable standalone as a CLI.
"""
import argparse
import csv
import sqlite3
import time
from pathlib import Path

from gqgmc import GMC320, GMCConfig, find_gmc_port
from history_parser import parse_history

DATA_DIR = Path(__file__).parent / "data"
RAW_PATH = DATA_DIR / "history_raw.bin"
CSV_PATH = DATA_DIR / "history.csv"
DB_PATH = DATA_DIR / "history.sqlite"


def download_raw(port: str, progress=None) -> bytes:
    cfg = GMCConfig(port=port, baud=19200)
    with GMC320(cfg) as dev:
        total = dev.get_flash_size()
        raw = dev.read_all_history(total_size=total, progress=progress)
        return raw


def save_raw(raw: bytes):
    DATA_DIR.mkdir(exist_ok=True)
    RAW_PATH.write_bytes(raw)


def save_parsed(samples):
    DATA_DIR.mkdir(exist_ok=True)

    with CSV_PATH.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "value", "is_cps", "cpm", "mode"])
        for s in samples:
            w.writerow(
                [s.timestamp.isoformat() if s.timestamp else "", s.cps_or_cpm, int(s.is_cps), s.cpm, s.mode]
            )

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS readings (
            timestamp TEXT, value INTEGER, is_cps INTEGER, cpm INTEGER, mode TEXT
        )
        """
    )
    conn.execute("DELETE FROM readings")
    conn.executemany(
        "INSERT INTO readings (timestamp, value, is_cps, cpm, mode) VALUES (?, ?, ?, ?, ?)",
        [
            (s.timestamp.isoformat() if s.timestamp else None, s.cps_or_cpm, int(s.is_cps), s.cpm, s.mode)
            for s in samples
        ],
    )
    conn.commit()
    conn.close()


def run_download(port: str, progress=None) -> dict:
    """Full pipeline: download, save raw, parse, save CSV+SQLite. Returns a summary dict."""
    t0 = time.time()
    raw = download_raw(port, progress=progress)
    save_raw(raw)

    samples = parse_history(raw)
    dated = [s for s in samples if s.timestamp is not None]
    save_parsed(samples)

    return {
        "bytes": len(raw),
        "samples": len(samples),
        "dated_samples": len(dated),
        "start": dated[0].timestamp.isoformat() if dated else None,
        "end": dated[-1].timestamp.isoformat() if dated else None,
        "elapsed_seconds": round(time.time() - t0, 1),
    }


def main():
    ap = argparse.ArgumentParser(description="Download GMC-320 history over serial")
    ap.add_argument("--port", default=None, help="Serial port (default: auto-detect)")
    args = ap.parse_args()

    port = args.port or find_gmc_port()
    print(f"Using port {port}")

    def progress(done, total):
        pct = 100 * done / total
        print(f"\r  {done:,}/{total:,} bytes ({pct:5.1f}%)", end="", flush=True)

    result = run_download(port, progress=progress)
    print()
    print(f"Downloaded {result['bytes']:,} bytes -> {result['samples']:,} samples "
          f"({result['dated_samples']:,} dated)")
    if result["start"]:
        print(f"  time range: {result['start']} -> {result['end']}")
    print(f"Saved to {CSV_PATH} and {DB_PATH}")
    print(f"Done in {result['elapsed_seconds']}s")


if __name__ == "__main__":
    main()
