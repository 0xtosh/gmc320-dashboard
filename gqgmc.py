"""
Serial client for GQ GMC-320 (and compatible) Geiger counters.

Confirmed against a real GMC-320Re 4.55 unit:
  - port: whatever the CH340/CP2102 USB-serial adapter enumerates as
  - baud: 19200  (NOT the commonly-documented 57600 -- varies by firmware/model)
  - 8N1, no flow control

Protocol reference: GQ-RFC1201 / GQ-RFC1801 (GQ Electronics serial command set).
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass

import serial
import serial.tools.list_ports

DEFAULT_BAUD = 19200

# USB VID:PID pairs for the common USB-serial chips GQ counters ship with
# (this GMC-320Re 4.55 uses the WCH CH340, VID_1A86/PID_7523).
KNOWN_SERIAL_CHIPS = {
    (0x1A86, 0x7523),  # WCH CH340
    (0x10C4, 0xEA60),  # Silicon Labs CP2102/CP2109
    (0x067B, 0x2303),  # Prolific PL2303
}


FLASH_PAGE_SIZE = 2048  # bytes per history read chunk that the device tolerates well
# NB: the GQ-RFC1201 spec says SPIR can request up to 4096B, but this unit
# (GMC-320Re 4.55) silently fails (returns ~1 byte) above 2048B in practice.


class GMCError(RuntimeError):
    pass


def find_gmc_port() -> str:
    """Auto-detect the GMC's serial port by known USB-serial chip VID:PID.

    Falls back to the only available port if exactly one is present.
    Raises if none or multiple candidates are found (caller should then
    let the user specify --port explicitly).
    """
    ports = list(serial.tools.list_ports.comports())
    candidates = [p for p in ports if (p.vid, p.pid) in KNOWN_SERIAL_CHIPS]
    if len(candidates) == 1:
        return candidates[0].device
    if not candidates and len(ports) == 1:
        return ports[0].device
    if len(candidates) > 1:
        names = ", ".join(p.device for p in candidates)
        raise GMCError(f"multiple candidate serial ports found ({names}); specify --port explicitly")
    raise GMCError("no GMC serial port found; specify --port explicitly")


@dataclass
class GMCConfig:
    port: str
    baud: int = DEFAULT_BAUD
    timeout: float = 2.0
    settle: float = 1.0  # seconds to wait after opening the port before talking


class GMC320:
    def __init__(self, cfg: GMCConfig):
        self.cfg = cfg
        self._ser: serial.Serial | None = None

    def __enter__(self) -> "GMC320":
        self._ser = serial.Serial(
            self.cfg.port, self.cfg.baud, timeout=self.cfg.timeout, write_timeout=self.cfg.timeout
        )
        time.sleep(self.cfg.settle)
        self._ser.reset_input_buffer()
        return self

    def __exit__(self, *exc):
        if self._ser:
            self._ser.close()

    # -- low level -----------------------------------------------------
    def _cmd(self, cmd: bytes, expect: int | None = None, read_all: bool = False) -> bytes:
        assert self._ser is not None
        self._ser.reset_input_buffer()
        self._ser.write(cmd)
        self._ser.flush()
        if read_all:
            time.sleep(0.2)
            buf = b""
            while True:
                chunk = self._ser.read(4096)
                if not chunk:
                    break
                buf += chunk
            return buf
        if expect is not None:
            return self._ser.read(expect)
        return self._ser.read(256)

    # -- basic info ------------------------------------------------------
    def get_version(self) -> str:
        resp = self._cmd(b"<GETVER>>", read_all=True)
        return resp.decode(errors="replace").strip()

    def get_cpm(self) -> int:
        resp = self._cmd(b"<GETCPM>>", expect=2)
        if len(resp) != 2:
            raise GMCError(f"GETCPM: expected 2 bytes, got {resp!r}")
        return struct.unpack(">H", resp)[0]

    def get_gyro(self) -> bytes:
        return self._cmd(b"<GETGYRO>>", expect=7)

    def get_voltage(self) -> float:
        resp = self._cmd(b"<GETVOLT>>", expect=1)
        if len(resp) != 1:
            raise GMCError(f"GETVOLT: expected 1 byte, got {resp!r}")
        return resp[0] / 10.0

    def get_temp(self) -> bytes:
        # Firmware/model dependent; not all units implement this meaningfully.
        return self._cmd(b"<GETTEMP>>", expect=4)

    def get_date_time(self) -> bytes:
        return self._cmd(b"<GETDATETIME>>", expect=7)

    def get_serial(self) -> bytes:
        return self._cmd(b"<GETSERIAL>>", expect=7)

    def get_config(self) -> bytes:
        return self._cmd(b"<GETCFG>>", read_all=True)

    # -- history flash -----------------------------------------------------
    def get_flash_size(self) -> int:
        """Returns total history flash size in bytes (1MB or 64KB depending on model)."""
        ver = self.get_version()
        # GMC-320 has 1MB (0x100000) history flash in most revisions.
        return 0x100000

    def read_history_chunk(self, addr: int, length: int) -> bytes:
        """<SPIR[A2][A1][A0][L1][L0]>> reads `length` bytes starting at 24-bit addr."""
        if length > FLASH_PAGE_SIZE:
            raise GMCError(f"length {length} exceeds safe chunk size {FLASH_PAGE_SIZE}")
        a2 = (addr >> 16) & 0xFF
        a1 = (addr >> 8) & 0xFF
        a0 = addr & 0xFF
        l1 = (length >> 8) & 0xFF
        l0 = length & 0xFF
        cmd = b"<SPIR" + bytes([a2, a1, a0, l1, l0]) + b">>"
        assert self._ser is not None
        self._ser.reset_input_buffer()
        self._ser.write(cmd)
        self._ser.flush()
        # pyserial's read(n) blocks until n bytes arrive or the port timeout
        # elapses; size the timeout generously for the requested length so
        # large chunks (up to 4096B) don't get truncated at ~19200 baud.
        old_timeout = self._ser.timeout
        self._ser.timeout = max(self.cfg.timeout, length / 1500 + 1.5)
        try:
            data = self._ser.read(length)
        finally:
            self._ser.timeout = old_timeout
        return data

    def read_all_history(
        self, total_size: int | None = None, progress=None, max_retries: int = 5
    ) -> bytes:
        """Download the full history flash. `progress(done, total)` optional callback."""
        total = total_size or self.get_flash_size()
        out = bytearray()
        addr = 0
        while addr < total:
            length = min(FLASH_PAGE_SIZE, total - addr)
            for attempt in range(max_retries):
                chunk = self.read_history_chunk(addr, length)
                if len(chunk) == length:
                    break
                time.sleep(0.3)
            else:
                raise GMCError(
                    f"short read at addr {addr:#x}: expected {length}, got {len(chunk)} "
                    f"after {max_retries} retries"
                )
            out += chunk
            addr += length
            if progress:
                progress(addr, total)
        return bytes(out)
