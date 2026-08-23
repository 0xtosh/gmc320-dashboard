"""
Parser for GQ GMC history flash data, downloaded via <SPIR...>> commands.

Format (reverse engineered / confirmed against GeigerLog's ghist.py, a mature
open-source implementation of the GQ protocol):

The flash is written as a stream of records:
  - A plain byte (not part of a 0x55 0xAA marker) is a single CPS or CPM
    sample, value = the byte itself (0-254).
  - A marker `55 AA <type> ...` changes parser state or injects a special
    value:
      type 0x00 : timestamp + save-mode change.
                  55 AA 00 YY MM DD hh mm ss 55 AA <mode>
                  mode: 0=off, 1=CPS/1s, 2=CPM/1min, 3=CPM/1hr,
                        4=CPS threshold, 5=CPM threshold
      type 0x01 : 2-byte big-endian value (extends the 1-byte range).
                  55 AA 01 MSB LSB

The flash is used as a (roughly) linear buffer starting near address 0, but
to be robust to circular wraparound we rotate the buffer to start at the
first valid timestamp marker found, append the wrapped prefix to the end,
then strip trailing erased (0xFF) padding -- same approach GeigerLog uses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

DATETIME_TAG = re.compile(rb"\x55\xaa\x00......\x55\xaa[\x00\x01\x02\x03]", re.DOTALL)

MODE_NAMES = {
    0: "off",
    1: "CPS-every-second",
    2: "CPM-every-minute",
    3: "CPM-hourly",
    4: "CPS-threshold",
    5: "CPM-threshold",
}
# seconds between samples for each mode (used to advance the running clock)
MODE_INTERVAL = {
    0: None,
    1: 1,
    2: 60,
    3: 3600,
    4: 1,
    5: 60,
}
# whether samples in this mode are CPS (need summing into a 60s window for CPM)
MODE_IS_CPS = {0: False, 1: True, 2: False, 3: False, 4: True, 5: False}


@dataclass
class Sample:
    timestamp: datetime
    cps_or_cpm: int
    is_cps: bool
    cpm: int  # derived CPM (rolling sum of last 60 CPS samples if is_cps, else same as cps_or_cpm)
    mode: str


def align_to_first_marker(raw: bytes) -> bytes:
    # Strip erased/unwritten flash (0xFF filler) BEFORE rotating: it sits at
    # the true physical end of a not-yet-wrapped log, but a naive rotate+strip
    # (rotate first, strip last) can leave it stranded in the middle of the
    # array if the first marker isn't at byte 0 -- producing thousands of
    # bogus 0xFF "255 CPS" samples that were actually just empty flash.
    raw = raw.rstrip(b"\xff")
    m = DATETIME_TAG.search(raw)
    if not m:
        return raw
    start = m.start()
    return raw[start:] + raw[:start]


def parse_history(raw: bytes) -> list[Sample]:
    data = align_to_first_marker(raw)
    n = len(data)
    samples: list[Sample] = []

    ts: datetime | None = None
    mode = 0
    interval = None
    is_cps = False
    step = 0  # how many samples since the last timestamp marker
    cps_window: list[int] = [0] * 60

    i = 0
    while i < n:
        b = data[i]
        if b == 0x55 and i + 1 < n and data[i + 1] == 0xAA:
            mtype = data[i + 2] if i + 2 < n else None
            if mtype == 0x00 and i + 11 < n and data[i + 9] == 0x55 and data[i + 10] == 0xAA:
                yy, mm, dd, hh, mi, ss = data[i + 3 : i + 9]
                try:
                    ts = datetime(2000 + yy, mm, dd, hh, mi, ss)
                except ValueError:
                    ts = ts  # corrupt timestamp; keep previous clock rather than crash
                mode = data[i + 11]
                interval = MODE_INTERVAL.get(mode)
                is_cps = MODE_IS_CPS.get(mode, False)
                step = 0
                cps_window = [0] * 60
                i += 12
                continue
            elif mtype == 0x01 and i + 4 < n:
                msb, lsb = data[i + 3], data[i + 4]
                value = (msb << 8) | lsb
                if is_cps:
                    value &= 0x3FFF
                samples.append(_make_sample(ts, interval, step, value, is_cps, mode, cps_window))
                step += 1
                i += 5
                continue
            else:
                # Unrecognized/rare marker (notes, tube-select, etc.) -- skip
                # the 3-byte prefix defensively rather than misparsing.
                i += 3
                continue
        elif b == 0xFF:
            # Erased/unwritten flash filler, not a real reading -- skip it
            # rather than recording a bogus "255" sample.
            i += 1
        else:
            samples.append(_make_sample(ts, interval, step, b, is_cps, mode, cps_window))
            step += 1
            i += 1

    return samples


def _make_sample(ts, interval, step, value, is_cps, mode, cps_window) -> Sample:
    when = ts + timedelta(seconds=step * interval) if ts and interval else ts
    if is_cps:
        cps_window.append(value)
        del cps_window[0]
        cpm = sum(cps_window)
    else:
        cpm = value
    return Sample(
        timestamp=when,
        cps_or_cpm=value,
        is_cps=is_cps,
        cpm=cpm,
        mode=MODE_NAMES.get(mode, f"unknown({mode})"),
    )
