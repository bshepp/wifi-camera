#!/usr/bin/env python3
"""
WiFi Camera - GPS Time Source

Provides GPS time and position for:
- Accurate UTC timestamps
- Position metadata
- Future PPS synchronization support

Supports u-blox and other NMEA-compatible GPS receivers.
"""

import serial
import threading
import time
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Callable
from pathlib import Path


@dataclass
class GPSFix:
    """GPS fix data"""
    timestamp: float  # Unix timestamp (UTC)
    time_utc: str  # HH:MM:SS.ss
    date_utc: str  # YYYY-MM-DD
    latitude: float  # Decimal degrees (positive = North)
    longitude: float  # Decimal degrees (positive = East)
    altitude_m: float  # Meters above MSL
    num_satellites: int
    hdop: float  # Horizontal dilution of precision
    fix_quality: int  # 0=invalid, 1=GPS, 2=DGPS
    speed_knots: float
    valid: bool
    # System time (time.time()) at the moment the NMEA sentence completing
    # this fix was received from the serial port. Used so that
    # `system_recv_time - timestamp` reflects (clock offset + NMEA latency)
    # rather than (clock offset + NMEA latency + age-of-last-fix).
    system_recv_time: float = 0.0


class NMEAParser:
    """Parse NMEA sentences from GPS"""

    @staticmethod
    def parse_time(time_str: str) -> Optional[str]:
        """Parse NMEA time (HHMMSS.ss) to HH:MM:SS.ss"""
        if not time_str or len(time_str) < 6:
            return None
        try:
            hours = time_str[0:2]
            mins = time_str[2:4]
            secs = time_str[4:]
            return f"{hours}:{mins}:{secs}"
        except (ValueError, IndexError):
            return None

    @staticmethod
    def parse_date(date_str: str) -> Optional[str]:
        """Parse NMEA date (DDMMYY) to YYYY-MM-DD"""
        if not date_str or len(date_str) != 6:
            return None
        try:
            day = date_str[0:2]
            month = date_str[2:4]
            year = "20" + date_str[4:6]  # Assume 20xx
            return f"{year}-{month}-{day}"
        except (ValueError, IndexError):
            return None

    @staticmethod
    def parse_coord(coord_str: str, direction: str) -> Optional[float]:
        """Parse NMEA coordinate (DDDMM.MMMMM) to decimal degrees"""
        if not coord_str:
            return None
        try:
            # Find decimal point
            dot_idx = coord_str.index('.')
            # Degrees are everything before the last 2 digits before decimal
            deg_end = dot_idx - 2
            degrees = float(coord_str[:deg_end])
            minutes = float(coord_str[deg_end:])
            decimal = degrees + minutes / 60.0

            if direction in ('S', 'W'):
                decimal = -decimal
            return decimal
        except (ValueError, IndexError):
            return None

    @staticmethod
    def verify_checksum(sentence: str) -> bool:
        """Verify NMEA checksum"""
        if '*' not in sentence:
            return False
        try:
            data, checksum = sentence.split('*')
            data = data[1:]  # Remove leading $
            calculated = 0
            for char in data:
                calculated ^= ord(char)
            return calculated == int(checksum, 16)
        except (ValueError, IndexError):
            return False

    @staticmethod
    def parse_gga(sentence: str) -> Optional[dict]:
        """Parse GPGGA sentence (fix data)"""
        parts = sentence.split(',')
        if len(parts) < 15:
            return None
        try:
            return {
                'type': 'GGA',
                'time': NMEAParser.parse_time(parts[1]),
                'latitude': NMEAParser.parse_coord(parts[2], parts[3]),
                'longitude': NMEAParser.parse_coord(parts[4], parts[5]),
                'fix_quality': int(parts[6]) if parts[6] else 0,
                'num_satellites': int(parts[7]) if parts[7] else 0,
                'hdop': float(parts[8]) if parts[8] else 99.9,
                'altitude_m': float(parts[9]) if parts[9] else 0.0,
            }
        except (ValueError, IndexError):
            return None

    @staticmethod
    def parse_rmc(sentence: str) -> Optional[dict]:
        """Parse GPRMC sentence (recommended minimum)"""
        parts = sentence.split(',')
        if len(parts) < 12:
            return None
        try:
            return {
                'type': 'RMC',
                'time': NMEAParser.parse_time(parts[1]),
                'valid': parts[2] == 'A',
                'latitude': NMEAParser.parse_coord(parts[3], parts[4]),
                'longitude': NMEAParser.parse_coord(parts[5], parts[6]),
                'speed_knots': float(parts[7]) if parts[7] else 0.0,
                'date': NMEAParser.parse_date(parts[9]),
            }
        except (ValueError, IndexError):
            return None


class GPSReader:
    """Reads GPS data in background thread"""

    def __init__(self, device: str = "/dev/ttyACM0", baudrate: int = 9600):
        self.device = device
        self.baudrate = baudrate
        self.serial: Optional[serial.Serial] = None
        self.running = False
        self.thread: Optional[threading.Thread] = None

        # Current fix data
        self._lock = threading.Lock()
        self._last_fix: Optional[GPSFix] = None
        self._last_gga: Optional[dict] = None
        self._last_rmc: Optional[dict] = None

        # Callbacks
        self._on_fix: Optional[Callable[[GPSFix], None]] = None
        self._on_pps: Optional[Callable[[float], None]] = None

    def start(self) -> bool:
        """Start GPS reader thread"""
        try:
            self.serial = serial.Serial(
                self.device,
                self.baudrate,
                timeout=1.0
            )
        except serial.SerialException as e:
            print(f"GPS: Failed to open {self.device}: {e}")
            return False

        self.running = True
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        """Stop GPS reader"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)
        if self.serial:
            self.serial.close()

    def _read_loop(self):
        """Main read loop"""
        while self.running:
            try:
                line = self.serial.readline()
                # Capture system time as close as possible to the moment the
                # NMEA sentence finished arriving on the serial port; this is
                # what get_time_offset() uses to anchor the clock comparison.
                recv_time = time.time()
                if not line:
                    continue

                sentence = line.decode('ascii', errors='replace').strip()
                if not sentence.startswith('$'):
                    continue

                # Verify checksum
                if not NMEAParser.verify_checksum(sentence):
                    continue

                # Parse sentence
                if 'GGA' in sentence:
                    data = NMEAParser.parse_gga(sentence)
                    if data:
                        with self._lock:
                            self._last_gga = data
                        self._try_build_fix(recv_time)

                elif 'RMC' in sentence:
                    data = NMEAParser.parse_rmc(sentence)
                    if data:
                        with self._lock:
                            self._last_rmc = data
                        self._try_build_fix(recv_time)

            except Exception as e:
                if self.running:
                    time.sleep(0.1)

    def _try_build_fix(self, recv_time: float):
        """Try to build a complete fix from GGA and RMC data.

        recv_time is the system time captured when the NMEA sentence that
        triggered this build attempt finished arriving on the serial port.
        """
        with self._lock:
            if not self._last_gga or not self._last_rmc:
                return

            gga = self._last_gga
            rmc = self._last_rmc

            # Check if data is recent (same time)
            if gga.get('time') != rmc.get('time'):
                return

            # Build timestamp
            date_str = rmc.get('date')
            time_str = rmc.get('time')
            if not date_str or not time_str:
                return

            try:
                # Parse to datetime
                dt_str = f"{date_str} {time_str}"
                dt = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S")
                # Add fractional seconds
                if '.' in time_str:
                    frac = float('0.' + time_str.split('.')[1])
                    dt = dt.replace(tzinfo=timezone.utc)
                    unix_time = dt.timestamp() + frac
                else:
                    dt = dt.replace(tzinfo=timezone.utc)
                    unix_time = dt.timestamp()

            except ValueError:
                return

            fix = GPSFix(
                timestamp=unix_time,
                time_utc=time_str,
                date_utc=date_str,
                latitude=gga.get('latitude', 0.0),
                longitude=gga.get('longitude', 0.0),
                altitude_m=gga.get('altitude_m', 0.0),
                num_satellites=gga.get('num_satellites', 0),
                hdop=gga.get('hdop', 99.9),
                fix_quality=gga.get('fix_quality', 0),
                speed_knots=rmc.get('speed_knots', 0.0),
                valid=rmc.get('valid', False) and gga.get('fix_quality', 0) > 0,
                system_recv_time=recv_time,
            )

            self._last_fix = fix

            # Clear old data
            self._last_gga = None
            self._last_rmc = None

        # Callback outside lock
        if self._on_fix and fix.valid:
            self._on_fix(fix)

    def get_fix(self) -> Optional[GPSFix]:
        """Get most recent GPS fix"""
        with self._lock:
            return self._last_fix

    def get_time(self) -> Optional[float]:
        """Get GPS time as Unix timestamp"""
        fix = self.get_fix()
        if fix and fix.valid:
            return fix.timestamp
        return None

    def get_time_offset(self) -> Optional[float]:
        """
        Get offset between system time and GPS time.

        Returns: (system_recv_time - gps_time) in seconds, anchored at the
                 moment the NMEA sentence completing the last fix was received
                 (not at "now"). Positive means the system clock is ahead.

                 This is (true clock offset) + (NMEA transport latency from the
                 GPS instant to serial arrival). The NMEA latency is roughly
                 constant per receiver — for sub-ms accuracy a PPS sync is
                 required. Earlier versions of this method used time.time() at
                 the call site, which folded in up to a full second of
                 "age of last fix" jitter.
        """
        fix = self.get_fix()
        if fix and fix.valid and fix.system_recv_time > 0:
            return fix.system_recv_time - fix.timestamp
        return None

    def on_fix(self, callback: Callable[[GPSFix], None]):
        """Set callback for new GPS fix"""
        self._on_fix = callback

    def wait_for_fix(self, timeout: float = 30.0) -> Optional[GPSFix]:
        """Wait for a valid GPS fix"""
        start = time.time()
        while time.time() - start < timeout:
            fix = self.get_fix()
            if fix and fix.valid:
                return fix
            time.sleep(0.5)
        return None


def main():
    """Test GPS reading"""
    import sys

    device = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"

    print(f"Reading GPS from {device}...")
    gps = GPSReader(device)

    if not gps.start():
        print("Failed to start GPS reader")
        sys.exit(1)

    print("Waiting for fix...")
    fix = gps.wait_for_fix(timeout=60)

    if fix:
        print(f"\n=== GPS Fix ===")
        print(f"Time (UTC):    {fix.date_utc} {fix.time_utc}")
        print(f"Unix time:     {fix.timestamp:.3f}")
        print(f"Position:      {fix.latitude:.6f}, {fix.longitude:.6f}")
        print(f"Altitude:      {fix.altitude_m:.1f} m")
        print(f"Satellites:    {fix.num_satellites}")
        print(f"HDOP:          {fix.hdop:.2f}")
        print(f"Speed:         {fix.speed_knots:.1f} knots")

        offset = gps.get_time_offset()
        if offset is not None:
            print(f"\nSystem clock offset: {offset*1000:+.1f} ms")
            if abs(offset) > 1.0:
                print("  WARNING: System clock differs from GPS by >1 second")
    else:
        print("No fix obtained within timeout")

    # Monitor for a bit
    print("\nMonitoring (Ctrl+C to stop)...")
    try:
        while True:
            fix = gps.get_fix()
            if fix and fix.valid:
                offset = gps.get_time_offset()
                print(f"\r{fix.time_utc} | {fix.num_satellites} sats | "
                      f"offset: {offset*1000:+.1f}ms", end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n")

    gps.stop()
    print("Done")


if __name__ == "__main__":
    main()

