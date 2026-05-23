#!/usr/bin/env python3
"""
WiFi Camera - Synchronized Multi-Device Capture

Captures synchronized IQ data from:
- 2x RTL-SDR (surveillance channels, left/right)
- 1x HackRF One (reference channel, directional)
- 1x Webcam (visual reference)

With GPS timestamps for time alignment.
"""

import os
import sys
import time
import signal
import subprocess
import threading
import queue
import struct
import json
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Tuple, BinaryIO
import argparse

from config import CaptureConfig, DEFAULT_CONFIG
from devices import DeviceManager

# Optional GPS support
try:
    from gps import GPSReader, GPSFix
    GPS_AVAILABLE = True
except ImportError:
    GPS_AVAILABLE = False


@dataclass
class CaptureMetadata:
    """Metadata for a capture session"""
    session_id: str
    start_time_utc: str
    start_time_unix: float
    duration_seconds: float
    wifi_channel: int
    frequency_hz: int

    # Device info
    rtlsdr_left_index: Optional[int]
    rtlsdr_right_index: Optional[int]
    hackrf_serial: Optional[str]
    webcam_device: Optional[str]

    # Configuration
    rtlsdr_sample_rate: int
    rtlsdr_gain: float
    hackrf_sample_rate: int
    hackrf_lna_gain: int
    hackrf_vga_gain: int

    # Physical setup
    baseline_meters: float = 0.38

    # File info
    files: Dict[str, str] = None

    def __post_init__(self):
        if self.files is None:
            self.files = {}


class StreamCapture:
    """Captures streaming data from an SDR process"""

    def __init__(self, name: str, output_file: Path, buffer_size: int = 262144):
        self.name = name
        self.output_file = output_file
        self.buffer_size = buffer_size
        self.process: Optional[subprocess.Popen] = None
        self.file_handle: Optional[BinaryIO] = None
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self.bytes_written = 0
        self.samples_written = 0
        self.start_time: Optional[float] = None
        self.first_data_time: Optional[float] = None  # When first bytes actually received
        self.error: Optional[str] = None
        self._start_barrier: Optional[threading.Barrier] = None
        self._command: Optional[List[str]] = None

    def prepare(self, command: List[str], barrier: Optional[threading.Barrier] = None):
        """
        Prepare capture (open files) but don't start process yet.
        Used for coordinated start with barrier synchronization.
        """
        self._command = command
        self._start_barrier = barrier
        self.file_handle = open(self.output_file, 'wb')
        return True

    def start(self, command: List[str] = None):
        """Start capture process and writer thread"""
        # If prepare() wasn't called, do it now
        if self.file_handle is None:
            self.file_handle = open(self.output_file, 'wb')
        
        cmd = command or self._command
        if cmd is None:
            self.error = "No command specified"
            return False

        self.running = True
        self.start_time = time.time()

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=self.buffer_size
            )
        except FileNotFoundError as e:
            self.error = f"Command not found: {cmd[0]}"
            self.running = False
            return False

        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()
        return True
    
    def start_coordinated(self):
        """
        Start with barrier synchronization.
        All streams wait at barrier before starting their processes.
        """
        if self._command is None:
            self.error = "Must call prepare() before start_coordinated()"
            return False
        
        # Wait for all streams to be ready
        if self._start_barrier:
            try:
                self._start_barrier.wait(timeout=5.0)
            except threading.BrokenBarrierError:
                self.error = "Barrier synchronization failed"
                return False
        
        return self.start(self._command)

    def _capture_loop(self):
        """Main capture loop - reads from process, writes to file"""
        try:
            while self.running and self.process.poll() is None:
                data = self.process.stdout.read(self.buffer_size)
                if data:
                    # Record timestamp of first data received
                    if self.first_data_time is None:
                        self.first_data_time = time.time()
                    self.file_handle.write(data)
                    self.bytes_written += len(data)
                    self.samples_written = self.bytes_written // 2  # 8-bit IQ = 2 bytes/sample
                else:
                    time.sleep(0.001)
        except Exception as e:
            self.error = str(e)
        finally:
            self.running = False

    def stop(self):
        """Stop capture"""
        self.running = False

        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()

            # Capture any stderr
            if self.process.stderr:
                stderr = self.process.stderr.read()
                if stderr and b'error' in stderr.lower():
                    self.error = stderr.decode('utf-8', errors='replace')[:500]

        if self.thread:
            self.thread.join(timeout=2)

        if self.file_handle:
            self.file_handle.close()

    def get_status(self) -> Dict:
        """Get capture status"""
        elapsed = time.time() - self.start_time if self.start_time else 0
        return {
            "name": self.name,
            "running": self.running,
            "bytes_written": self.bytes_written,
            "samples_written": self.samples_written,
            "elapsed_seconds": elapsed,
            "data_rate_mbps": (self.bytes_written / elapsed / 1e6) if elapsed > 0 else 0,
            "first_data_time": self.first_data_time,
            "error": self.error,
        }


class WebcamCapture:
    """Captures individual frames from webcam with precise timestamps"""

    def __init__(self, output_dir: Path, device: str = "/dev/video0",
                 width: int = 1280, height: int = 720, fps: int = 30):
        self.output_dir = output_dir
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.process: Optional[subprocess.Popen] = None
        self.running = False
        self.frame_count = 0
        self.start_time: Optional[float] = None
        self.timestamps: List[Tuple[int, float]] = []  # (frame_num, timestamp)
        self.thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None
        self.frames_dir: Optional[Path] = None

    def start(self):
        """Start webcam capture with individual frames"""
        self.running = True
        self.start_time = time.time()
        self.timestamps = []
        self.frame_count = 0

        # Create frames subdirectory
        self.frames_dir = self.output_dir / "frames"
        self.frames_dir.mkdir(exist_ok=True)

        # Use ffmpeg to pipe raw frames to stdout
        # We'll read them in Python and save with timestamps
        command = [
            "ffmpeg",
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-video_size", f"{self.width}x{self.height}",
            "-framerate", str(self.fps),
            "-i", self.device,
            "-c:v", "copy",  # Keep as MJPEG
            "-f", "image2pipe",  # Pipe images
            "-"
        ]

        try:
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1024*1024  # 1MB buffer
            )
        except FileNotFoundError:
            self.error = "ffmpeg not found - install with: sudo apt install ffmpeg"
            self.running = False
            return False

        # Start frame capture thread
        self.thread = threading.Thread(target=self._capture_frames, daemon=True)
        self.thread.start()

        return True

    def _capture_frames(self):
        """Capture frames from ffmpeg pipe and save with timestamps"""
        # JPEG markers
        SOI = b'\xff\xd8'  # Start of image
        EOI = b'\xff\xd9'  # End of image

        buffer = b''

        try:
            while self.running and self.process.poll() is None:
                # Read chunk from ffmpeg
                chunk = self.process.stdout.read(65536)
                if not chunk:
                    time.sleep(0.001)
                    continue

                buffer += chunk

                # Find complete JPEG frames in buffer
                while True:
                    # Find start of JPEG
                    start = buffer.find(SOI)
                    if start == -1:
                        buffer = b''
                        break

                    # Find end of JPEG
                    end = buffer.find(EOI, start + 2)
                    if end == -1:
                        # Incomplete frame, keep buffer from start
                        buffer = buffer[start:]
                        break

                    # Extract complete JPEG frame
                    frame_data = buffer[start:end + 2]
                    buffer = buffer[end + 2:]

                    # Record timestamp
                    timestamp = time.time()
                    self.timestamps.append((self.frame_count, timestamp))

                    # Save frame with timestamp in filename
                    # Format: frame_NNNN_TIMESTAMP.jpg
                    filename = f"frame_{self.frame_count:06d}_{timestamp:.6f}.jpg"
                    frame_path = self.frames_dir / filename

                    with open(frame_path, 'wb') as f:
                        f.write(frame_data)

                    self.frame_count += 1

        except Exception as e:
            self.error = str(e)
        finally:
            self.running = False

    def stop(self):
        """Stop webcam capture and save timestamp index"""
        self.running = False

        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()

        if self.thread:
            self.thread.join(timeout=2)

        # Save timestamps to JSON for easy loading
        if self.timestamps and self.output_dir:
            timestamps_file = self.output_dir / "frame_timestamps.json"
            with open(timestamps_file, 'w') as f:
                json.dump({
                    "frame_count": self.frame_count,
                    "fps": self.fps,
                    "resolution": f"{self.width}x{self.height}",
                    "frames": [
                        {"frame": num, "timestamp": ts}
                        for num, ts in self.timestamps
                    ]
                }, f, indent=2)

    def get_status(self) -> Dict:
        """Get capture status"""
        elapsed = time.time() - self.start_time if self.start_time else 0
        return {
            "name": "webcam",
            "running": self.running,
            "frame_count": self.frame_count,
            "elapsed_seconds": elapsed,
            "fps_actual": self.frame_count / elapsed if elapsed > 0 else 0,
            "error": self.error,
        }


class WiFiCameraCapture:
    """Main capture coordinator"""

    def __init__(self, config: CaptureConfig):
        self.config = config
        self.device_manager = DeviceManager()
        self.captures: Dict[str, StreamCapture] = {}
        self.webcam: Optional[WebcamCapture] = None
        self.gps: Optional['GPSReader'] = None
        self.session_id: str = ""
        self.output_dir: Path = None
        self.metadata: Optional[CaptureMetadata] = None
        self.running = False
        self.gps_offset: Optional[float] = None  # System time - GPS time

    def setup(self) -> bool:
        """Detect devices and prepare for capture"""
        print("Detecting devices...")
        self.device_manager.detect_all()
        self.device_manager.print_summary()

        # Validate required devices
        if self.config.capture_rtlsdr and len(self.device_manager.rtlsdr_devices) < 2:
            print("ERROR: Need 2 RTL-SDR devices for capture")
            return False

        if self.config.capture_hackrf and not self.device_manager.hackrf_device:
            print("ERROR: HackRF not found")
            return False

        if self.config.capture_webcam and not self.device_manager.webcam_device:
            print("WARNING: Webcam not found, disabling webcam capture")
            self.config.capture_webcam = False

        # Initialize GPS if available
        if GPS_AVAILABLE and self.config.capture_gps and self.device_manager.gps_device:
            print("\nInitializing GPS...")
            self.gps = GPSReader(
                device=self.device_manager.gps_device.device_path,
                baudrate=self.config.gps.baudrate
            )
            if self.gps.start():
                # Wait briefly for fix
                fix = self.gps.wait_for_fix(timeout=5.0)
                if fix:
                    self.gps_offset = self.gps.get_time_offset()
                    print(f"  GPS fix: {fix.num_satellites} satellites, HDOP {fix.hdop:.1f}")
                    print(f"  System clock offset: {self.gps_offset*1000:+.1f} ms from GPS")
                else:
                    print("  GPS: No fix yet (will continue attempting)")
            else:
                print("  GPS: Failed to initialize")
                self.gps = None

        # Create session directory
        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.output_dir = Path(self.config.output_dir) / self.session_id
        self.output_dir.mkdir(parents=True, exist_ok=True)

        print(f"\nSession: {self.session_id}")
        print(f"Output: {self.output_dir}")

        return True

    def _build_rtlsdr_command(self, device_index: int, output_file: Path) -> List[str]:
        """Build rtl_sdr command for capture"""
        cfg = self.config.rtlsdr
        cmd = [
            "rtl_sdr",
            "-d", str(device_index),
            "-f", str(cfg.frequency),
            "-s", str(cfg.sample_rate),
        ]
        # Only add -g flag if gain > 0 (use automatic gain otherwise)
        if cfg.gain > 0:
            cmd.extend(["-g", str(int(cfg.gain))])
        cmd.extend([
            "-p", str(cfg.ppm_correction),
            "-",  # Output to stdout
        ])
        return cmd

    def _build_hackrf_command(self, output_file: Path) -> List[str]:
        """Build hackrf_transfer command for capture"""
        cfg = self.config.hackrf
        return [
            "hackrf_transfer",
            "-r", "-",  # Receive to stdout
            "-f", str(cfg.frequency),
            "-s", str(cfg.sample_rate),
            "-l", str(cfg.lna_gain),
            "-g", str(cfg.vga_gain),
            "-a", "1" if cfg.amp_enable else "0",
        ]

    def start(self, coordinated: bool = True):
        """
        Start all captures.
        
        Args:
            coordinated: If True, use barrier synchronization for tighter timing.
                        All SDR processes start within ~1ms of each other.
        """
        self.running = True
        start_time_utc = datetime.now(timezone.utc)

        # Get device info for metadata
        rtl_left = self.device_manager.get_rtlsdr_by_position("left")
        rtl_right = self.device_manager.get_rtlsdr_by_position("right")

        # Create metadata
        self.metadata = CaptureMetadata(
            session_id=self.session_id,
            start_time_utc=start_time_utc.isoformat(),
            start_time_unix=time.time(),
            duration_seconds=self.config.duration_seconds,
            wifi_channel=self.config.wifi_channel,
            frequency_hz=self.config.rtlsdr.frequency,
            rtlsdr_left_index=rtl_left.index if rtl_left else None,
            rtlsdr_right_index=rtl_right.index if rtl_right else None,
            hackrf_serial=self.device_manager.hackrf_device.serial if self.device_manager.hackrf_device else None,
            webcam_device=self.device_manager.webcam_device.device_path if self.device_manager.webcam_device else None,
            rtlsdr_sample_rate=self.config.rtlsdr.sample_rate,
            rtlsdr_gain=self.config.rtlsdr.gain,
            hackrf_sample_rate=self.config.hackrf.sample_rate,
            hackrf_lna_gain=self.config.hackrf.lna_gain,
            hackrf_vga_gain=self.config.hackrf.vga_gain,
        )

        print(f"\nStarting capture for {self.config.duration_seconds} seconds...")
        print(f"Frequency: {self.config.rtlsdr.frequency / 1e6:.1f} MHz (Channel {self.config.wifi_channel})")

        # Count how many SDR streams we'll have for the barrier
        num_sdr_streams = 0
        if self.config.capture_rtlsdr:
            if rtl_left:
                num_sdr_streams += 1
            if rtl_right:
                num_sdr_streams += 1
        if self.config.capture_hackrf and self.device_manager.hackrf_device:
            num_sdr_streams += 1

        # Create barrier for coordinated start (+1 for main thread)
        barrier = threading.Barrier(num_sdr_streams + 1) if coordinated and num_sdr_streams > 1 else None
        start_threads = []

        # Prepare RTL-SDR captures
        if self.config.capture_rtlsdr:
            if rtl_left:
                output_file = self.output_dir / "rtlsdr_left.bin"
                self.metadata.files["rtlsdr_left"] = output_file.name
                capture = StreamCapture("RTL-SDR LEFT", output_file)
                cmd = self._build_rtlsdr_command(rtl_left.index, output_file)
                capture.prepare(cmd, barrier)
                self.captures["rtlsdr_left"] = capture
                if coordinated and barrier:
                    t = threading.Thread(target=capture.start_coordinated, daemon=True)
                    start_threads.append(("RTL-SDR LEFT", t, rtl_left.index))

            if rtl_right:
                output_file = self.output_dir / "rtlsdr_right.bin"
                self.metadata.files["rtlsdr_right"] = output_file.name
                capture = StreamCapture("RTL-SDR RIGHT", output_file)
                cmd = self._build_rtlsdr_command(rtl_right.index, output_file)
                capture.prepare(cmd, barrier)
                self.captures["rtlsdr_right"] = capture
                if coordinated and barrier:
                    t = threading.Thread(target=capture.start_coordinated, daemon=True)
                    start_threads.append(("RTL-SDR RIGHT", t, rtl_right.index))

        # Prepare HackRF capture
        if self.config.capture_hackrf and self.device_manager.hackrf_device:
            output_file = self.output_dir / "hackrf.bin"
            self.metadata.files["hackrf"] = output_file.name
            capture = StreamCapture("HackRF", output_file)
            cmd = self._build_hackrf_command(output_file)
            capture.prepare(cmd, barrier)
            self.captures["hackrf"] = capture
            if coordinated and barrier:
                t = threading.Thread(target=capture.start_coordinated, daemon=True)
                start_threads.append(("HackRF", t, f"{self.config.hackrf.sample_rate // 1_000_000} MSPS"))

        # Start all SDR streams with coordinated timing
        if coordinated and barrier and start_threads:
            print("  Preparing coordinated start...")
            # Start all threads (they'll wait at the barrier)
            for name, t, info in start_threads:
                t.start()
            
            # Small delay to ensure all threads reach the barrier
            time.sleep(0.1)
            
            # Release the barrier - all processes start simultaneously
            coordinated_start_time = time.time()
            try:
                barrier.wait(timeout=5.0)
            except threading.BrokenBarrierError:
                print("  WARNING: Barrier synchronization failed, some streams may have timing issues")
            
            # Wait for threads to complete startup
            for name, t, info in start_threads:
                t.join(timeout=2.0)
                print(f"  Started {name} (device {info})")
            
            print(f"  Coordinated start at {coordinated_start_time:.6f}")
        else:
            # Sequential start (original behavior)
            for name, capture in self.captures.items():
                if capture.start():
                    print(f"  Started {name}")

        # Start webcam capture (individual frames with timestamps)
        if self.config.capture_webcam and self.device_manager.webcam_device:
            self.metadata.files["frames"] = "frames/"
            self.metadata.files["frame_timestamps"] = "frame_timestamps.json"
            self.webcam = WebcamCapture(
                self.output_dir,
                device=self.device_manager.webcam_device.device_path,
                width=self.config.webcam.width,
                height=self.config.webcam.height,
                fps=self.config.webcam.fps,
            )
            if self.webcam.start():
                print(f"  Started Webcam ({self.config.webcam.width}x{self.config.webcam.height} @ {self.config.webcam.fps}fps, individual frames)")

        # Record actual start time after all devices initialized
        self.metadata.start_time_unix = time.time()

    def monitor(self):
        """Monitor capture progress"""
        start_time = time.time()
        duration = self.config.duration_seconds

        try:
            while self.running and (time.time() - start_time) < duration:
                elapsed = time.time() - start_time
                remaining = duration - elapsed

                # Get status from all captures
                status_line = f"\r[{elapsed:.1f}s / {duration:.1f}s] "

                for name, capture in self.captures.items():
                    status = capture.get_status()
                    mb = status["bytes_written"] / 1e6
                    rate = status["data_rate_mbps"]
                    status_line += f"{name}: {mb:.1f}MB ({rate:.1f}MB/s) | "

                print(status_line, end="", flush=True)
                time.sleep(0.5)

        except KeyboardInterrupt:
            print("\n\nInterrupted!")

        print("\n")

    def stop(self):
        """Stop all captures"""
        self.running = False
        stop_time = time.time()
        print("Stopping captures...")

        # Collect timing info from all streams
        stream_timing = {}

        for name, capture in self.captures.items():
            capture.stop()
            status = capture.get_status()
            print(f"  {name}: {status['bytes_written'] / 1e6:.2f} MB")

            # Record per-stream timing
            stream_timing[name] = {
                "first_data_time": status.get("first_data_time"),
                "samples_written": status.get("samples_written", 0),
                "bytes_written": status.get("bytes_written", 0),
            }

        if self.webcam:
            self.webcam.stop()
            print(f"  webcam: {self.webcam.frame_count} frames")
            stream_timing["webcam"] = {
                "first_data_time": self.webcam.timestamps[0][1] if self.webcam.timestamps else None,
                "frame_count": self.webcam.frame_count,
            }

        # Calculate sample loss for each stream
        sample_loss = {}
        print("\n  Sample integrity:")
        for name, data in stream_timing.items():
            if name == "webcam":
                continue
            first_time = data.get("first_data_time")
            samples = data.get("samples_written", 0)
            if first_time and samples:
                stream_duration = stop_time - first_time
                if "rtl" in name:
                    expected = stream_duration * self.config.rtlsdr.sample_rate
                else:  # hackrf
                    expected = stream_duration * self.config.hackrf.sample_rate
                loss_pct = (1 - samples / expected) * 100 if expected > 0 else 0
                sample_loss[name] = loss_pct
                status = "✓" if abs(loss_pct) < 1 else "⚠" if abs(loss_pct) < 5 else "✗"
                print(f"    {status} {name}: {loss_pct:+.2f}% {'loss' if loss_pct > 0 else 'extra'}")

        # Get final GPS offset if available
        gps_info = {}
        if self.gps:
            fix = self.gps.get_fix()
            if fix and fix.valid:
                self.gps_offset = self.gps.get_time_offset()
                gps_info = {
                    "available": True,
                    "system_clock_offset_seconds": self.gps_offset,
                    "satellites": fix.num_satellites,
                    "hdop": fix.hdop,
                    "latitude": fix.latitude,
                    "longitude": fix.longitude,
                }
                print(f"\n  GPS: {fix.num_satellites} sats, offset {self.gps_offset*1000:+.1f}ms")
            else:
                gps_info = {"available": True, "fix": False}
            self.gps.stop()

        # Save timing synchronization file
        timing_file = self.output_dir / "timing.json"
        timing_data = {
            "session_id": self.session_id,
            "capture_start_time": self.metadata.start_time_unix,
            "capture_stop_time": stop_time,
            "streams": stream_timing,
            "sample_rates": {
                "rtlsdr": self.config.rtlsdr.sample_rate,
                "hackrf": self.config.hackrf.sample_rate,
                "webcam_fps": self.config.webcam.fps,
            },
            "sample_loss_percent": sample_loss,
            "gps": gps_info,
            "notes": "first_data_time is when first bytes were received from each device"
        }
        with open(timing_file, 'w') as f:
            json.dump(timing_data, f, indent=2)
        print(f"\nTiming saved: {timing_file}")

        # Save metadata
        metadata_file = self.output_dir / "metadata.json"
        with open(metadata_file, 'w') as f:
            json.dump(asdict(self.metadata), f, indent=2)
        print(f"Metadata saved: {metadata_file}")

        # Summary
        print(f"\nCapture complete!")
        print(f"Output directory: {self.output_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="WiFi Camera - Synchronized SDR Capture"
    )
    parser.add_argument(
        "--duration", "-d", type=float, default=10.0,
        help="Capture duration in seconds (default: 10)"
    )
    parser.add_argument(
        "--channel", "-c", type=int, default=6,
        help="WiFi channel to capture (default: 6)"
    )
    parser.add_argument(
        "--no-hackrf", action="store_true",
        help="Disable HackRF capture"
    )
    parser.add_argument(
        "--no-webcam", action="store_true",
        help="Disable webcam capture"
    )
    parser.add_argument(
        "--output", "-o", type=str,
        help="Output directory (default: ~/Projects/wifi-camera/data)"
    )
    parser.add_argument(
        "--gain", "-g", type=float, default=None,
        help="RTL-SDR gain in dB (default: auto gain)"
    )

    args = parser.parse_args()

    # Build configuration
    config = CaptureConfig()
    config.duration_seconds = args.duration
    config.wifi_channel = args.channel
    config.capture_hackrf = not args.no_hackrf
    config.capture_webcam = not args.no_webcam
    if args.gain is not None:
        config.rtlsdr.gain = args.gain

    if args.output:
        config.output_dir = args.output

    # Create and run capture
    capture = WiFiCameraCapture(config)

    if not capture.setup():
        sys.exit(1)

    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        capture.running = False

    signal.signal(signal.SIGINT, signal_handler)

    capture.start()
    capture.monitor()
    capture.stop()


if __name__ == "__main__":
    main()
