"""Re-parse the already-downloaded raw flash dump (no serial access needed)."""
from history_parser import parse_history
from download_history import RAW_PATH, save_parsed, CSV_PATH, DB_PATH

raw = RAW_PATH.read_bytes()
samples = parse_history(raw)
dated = [s for s in samples if s.timestamp is not None]
print(f"Parsed {len(samples):,} samples ({len(dated):,} dated)")
if dated:
    ts_sorted = sorted(s.timestamp for s in dated)
    print(f"  time range: {ts_sorted[0]} -> {ts_sorted[-1]}")
save_parsed(samples)
print(f"Saved {CSV_PATH} and {DB_PATH}")
