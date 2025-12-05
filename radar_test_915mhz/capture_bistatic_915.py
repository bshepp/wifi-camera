#!/usr/bin/env python3
"""
915 MHz Bistatic Radar - Synchronized Multi-Device Capture

Captures synchronized IQ data from:
- 2x RTL-SDR (surveillance channels, left/right)
- 1x Webcam (visual ground truth)
- HackRF One (transmitting beacon in background)

With precise timing for passive radar processing.
"""

import os
import sys
import time
import signal
import subprocess
import threading
import json
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, BinaryIO
import argparse

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from devices import DeviceManager


@dataclass
class RadarMetadata:
    """Metadata for a 915 MHz bistatic radar capture session"""
    session_id: str
    start_time_utc: str
    start_time_unix: float
    duration_seconds: float
    frequency_hz: int = 915000000  # 915 MHz ISM band

    # Device info
    rtlsdr_left_index: Optional[int] = None
    rtlsdr_right_index: Optional[int] = None
    webcam_device: Optional[str] = None
    hackrf_serial: Optional[str] = None

    # Configuration
    rtlsdr_sample_rate: int = 2560000  # 2.56 MSPS
    rtlsdr_gain: str = "auto"
    hackrf_tx_gain: int = 20  # dB
    hackrf_tx_amp: bool = True

    # Physical setup
    baseline_meters: float = 0.38  # Distance between RTL-SDRs


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
        self.first_data_time: Optional[float] = None  # Critical for sync!
        self.error: Optional[str] = None
        self._start_barrier: Optional[threading.Barrier] = None
        self._command: Optional[List[str]] = None

    def prepare(self, command: List[str], barrier: Optional[threading.Barrier] = None):
        """Prepare capture (open files) but don't start process yet"""
        self._command = command
        self._start_barrier = barrier
        self.file_handle = open(self.output_file, 'wb')
        return True

    def start_coordinated(self):
        """Start with barrier synchronization"""
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

        self.running = True
        self.start_time = time.time()

        try:
            self.process = subprocess.Popen(
                self._command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=self.buffer_size
            )
        except FileNotFoundError as e:
            self.error = f"Command not found: {self._command[0]}"
            self.running = False
            return False

        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()
        return True

    def _capture_loop(self):
        """Main capture loop - reads from process, writes to file"""
        try:
            while self.running and self.process.poll() is None:
                data = self.process.stdout.read(self.buffer_size)
                if data:
                    # Record timestamp of first data received (CRITICAL!)
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
            "elapsed": elapsed,
            "error": self.error
        }


class WebcamCapture:
    """Captures webcam frames with timestamps"""

    def __init__(self, output_dir: Path, device: str = "/dev/video0"):
        self.output_dir = output_dir
        self.device = device
        self.process: Optional[subprocess.Popen] = None
        self.running = False
        self.start_time: Optional[float] = None
        self.first_frame_time: Optional[float] = None
        self.frame_timestamps: List[Dict] = []
        self.frame_count = 0
        self._start_barrier: Optional[threading.Barrier] = None
        self._command: Optional[List[str]] = None

    def prepare(self, duration: int, barrier: Optional[threading.Barrier] = None):
        """Prepare webcam capture"""
        self._start_barrier = barrier
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # FFmpeg command with timestamp metadata
        self._command = [
            'ffmpeg',
            '-f', 'v4l2',
            '-framerate', '30',
            '-video_size', '1280x720',
            '-i', self.device,
            '-t', str(duration),
            '-q:v', '2',
            '-frame_pts', '1',  # Include PTS in output
            f'{self.output_dir}/frame_%06d.jpg'
        ]
        return True

    def start_coordinated(self):
        """Start with barrier synchronization"""
        if self._start_barrier:
            try:
                self._start_barrier.wait(timeout=5.0)
            except threading.BrokenBarrierError:
                return False

        self.running = True
        self.start_time = time.time()
        self.first_frame_time = self.start_time  # Approximate

        try:
            self.process = subprocess.Popen(
                self._command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
        except FileNotFoundError:
            self.running = False
            return False

        return True

    def stop(self):
        """Stop webcam capture"""
        self.running = False

        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()

        # Generate frame timestamps (approximation based on 30 fps)
        frame_files = sorted(self.output_dir.glob("frame_*.jpg"))
        self.frame_count = len(frame_files)

        if self.frame_count > 0 and self.first_frame_time:
            frame_interval = 1.0 / 30.0  # 30 fps
            for i, frame_file in enumerate(frame_files):
                timestamp = self.first_frame_time + (i * frame_interval)
                self.frame_timestamps.append({
                    "frame": i,
                    "filename": frame_file.name,
                    "timestamp": timestamp
                })


def start_hackrf_beacon(beacon_file: Path, frequency: int = 915000000) -> subprocess.Popen:
    """Start HackRF transmitting beacon in background"""
    cmd = [
        'hackrf_transfer',
        '-t', str(beacon_file),
        '-f', str(frequency),
        '-s', '10000000',  # 10 MSPS
        '-x', '20',  # TX gain
        '-a', '1',  # TX amp enable
        '-R'  # Repeat
    ]

    print(f"Starting HackRF beacon: {' '.join(cmd)}")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )

    # Give it time to start
    time.sleep(2)

    if process.poll() is not None:
        raise RuntimeError("HackRF beacon failed to start")

    return process


def main():
    parser = argparse.ArgumentParser(description='915 MHz Bistatic Radar Capture')
    parser.add_argument('--duration', type=int, default=30, help='Capture duration in seconds')
    parser.add_argument('--beacon-file', type=str, default='../beacon_900mhz_cw.iq',
                        help='HackRF beacon IQ file')
    parser.add_argument('--frequency', type=int, default=915000000,
                        help='Frequency in Hz (default: 915 MHz)')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Output directory (default: data/TIMESTAMP)')
    args = parser.parse_args()

    # Create session directory
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = Path(__file__).parent / "data" / session_id

    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = output_dir / "frames"

    print(f"=== 915 MHz Bistatic Radar Capture ===")
    print(f"Session: {session_id}")
    print(f"Duration: {args.duration}s")
    print(f"Frequency: {args.frequency / 1e6} MHz")
    print(f"Output: {output_dir}")
    print()

    # Detect devices
    print("Detecting devices...")
    dev_mgr = DeviceManager()
    rtlsdr_devices = dev_mgr.detect_rtlsdr()
    webcam = dev_mgr.detect_webcam()
    hackrf = dev_mgr.detect_hackrf()

    if len(rtlsdr_devices) < 2:
        print(f"ERROR: Need 2 RTL-SDR devices, found {len(rtlsdr_devices)}")
        sys.exit(1)

    # Find left and right RTL-SDRs
    rtlsdr_left = next((d for d in rtlsdr_devices if d.position == "left"), None)
    rtlsdr_right = next((d for d in rtlsdr_devices if d.position == "right"), None)

    if not rtlsdr_left or not rtlsdr_right:
        print("ERROR: Could not identify left/right RTL-SDR positions")
        sys.exit(1)

    print(f"✓ RTL-SDR Left: {rtlsdr_left.name} (index {rtlsdr_left.index})")
    print(f"✓ RTL-SDR Right: {rtlsdr_right.name} (index {rtlsdr_right.index})")

    if webcam:
        print(f"✓ Webcam: {webcam.device_path}")
    else:
        print("⚠ Webcam not found")

    if hackrf:
        print(f"✓ HackRF: {hackrf.serial}")
    else:
        print("⚠ HackRF not found (needed for beacon)")
        sys.exit(1)

    print()

    # Start HackRF beacon transmission
    beacon_file = Path(args.beacon_file)
    if not beacon_file.exists():
        print(f"ERROR: Beacon file not found: {beacon_file}")
        sys.exit(1)

    print("Starting HackRF beacon transmission...")
    hackrf_beacon = start_hackrf_beacon(beacon_file, args.frequency)
    print("✓ Beacon transmitting\n")

    # Create metadata
    metadata = RadarMetadata(
        session_id=session_id,
        start_time_utc=datetime.now(timezone.utc).isoformat(),
        start_time_unix=time.time(),
        duration_seconds=args.duration,
        frequency_hz=args.frequency,
        rtlsdr_left_index=rtlsdr_left.index,
        rtlsdr_right_index=rtlsdr_right.index,
        webcam_device=webcam.device_path if webcam else None,
        hackrf_serial=hackrf.serial if hackrf else None
    )

    # Setup synchronized captures
    num_streams = 3 if webcam else 2  # RTL-SDR left, right, webcam
    barrier = threading.Barrier(num_streams)

    # Calculate total samples for duration
    total_samples = args.duration * metadata.rtlsdr_sample_rate
    total_bytes = total_samples * 2  # IQ pairs

    # Create capture objects
    cap_left = StreamCapture("rtlsdr_left", output_dir / "rtlsdr_left.bin")
    cap_right = StreamCapture("rtlsdr_right", output_dir / "rtlsdr_right.bin")

    # RTL-SDR commands
    cmd_left = [
        'rtl_sdr',
        '-d', str(rtlsdr_left.index),
        '-f', str(args.frequency),
        '-s', str(metadata.rtlsdr_sample_rate),
        '-g', metadata.rtlsdr_gain,
        '-n', str(total_bytes),
        '-'  # Output to stdout
    ]

    cmd_right = [
        'rtl_sdr',
        '-d', str(rtlsdr_right.index),
        '-f', str(args.frequency),
        '-s', str(metadata.rtlsdr_sample_rate),
        '-g', metadata.rtlsdr_gain,
        '-n', str(total_bytes),
        '-'
    ]

    # Prepare captures
    cap_left.prepare(cmd_left, barrier)
    cap_right.prepare(cmd_right, barrier)

    if webcam:
        cap_webcam = WebcamCapture(frames_dir, webcam.device_path)
        cap_webcam.prepare(args.duration, barrier)
    else:
        cap_webcam = None

    # Setup signal handler for clean shutdown
    def signal_handler(sig, frame):
        print("\nInterrupt received, stopping capture...")
        cap_left.stop()
        cap_right.stop()
        if cap_webcam:
            cap_webcam.stop()
        hackrf_beacon.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # Start synchronized capture
    print("Starting synchronized capture...")
    capture_start_time = time.time()

    # Start all captures (they wait at barrier)
    threads = []
    threads.append(threading.Thread(target=cap_left.start_coordinated))
    threads.append(threading.Thread(target=cap_right.start_coordinated))
    if cap_webcam:
        threads.append(threading.Thread(target=cap_webcam.start_coordinated))

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    print(f"✓ All captures started (barrier synchronized)\n")

    # Monitor capture progress
    print(f"Capturing for {args.duration} seconds...")
    print("Move around in front of the antennas!\n")

    start = time.time()
    while time.time() - start < args.duration + 2:  # +2s buffer
        time.sleep(1)
        elapsed = time.time() - start

        # Show progress
        if elapsed <= args.duration:
            progress = (elapsed / args.duration) * 100
            print(f"\rProgress: {progress:5.1f}% ({elapsed:5.1f}s / {args.duration}s)", end='')

    print("\n\nStopping captures...")
    capture_stop_time = time.time()

    # Stop everything
    cap_left.stop()
    cap_right.stop()
    if cap_webcam:
        cap_webcam.stop()

    time.sleep(1)

    # Stop beacon
    print("Stopping HackRF beacon...")
    hackrf_beacon.terminate()
    try:
        hackrf_beacon.wait(timeout=2)
    except subprocess.TimeoutExpired:
        hackrf_beacon.kill()

    print("\n=== Capture Complete ===\n")

    # Generate timing.json
    timing_data = {
        "session_id": session_id,
        "capture_start_time": capture_start_time,
        "capture_stop_time": capture_stop_time,
        "duration": capture_stop_time - capture_start_time,
        "sample_rates": {
            "rtlsdr": metadata.rtlsdr_sample_rate
        },
        "streams": {
            "rtlsdr_left": {
                "first_data_time": cap_left.first_data_time,
                "samples_written": cap_left.samples_written,
                "bytes_written": cap_left.bytes_written
            },
            "rtlsdr_right": {
                "first_data_time": cap_right.first_data_time,
                "samples_written": cap_right.samples_written,
                "bytes_written": cap_right.bytes_written
            }
        }
    }

    # Calculate sample loss
    expected_samples = args.duration * metadata.rtlsdr_sample_rate
    for stream_name, stream_data in timing_data["streams"].items():
        actual = stream_data["samples_written"]
        loss_pct = ((expected_samples - actual) / expected_samples) * 100
        stream_data["expected_samples"] = expected_samples
        stream_data["sample_loss_percent"] = round(loss_pct, 2)

    with open(output_dir / "timing.json", 'w') as f:
        json.dump(timing_data, f, indent=2)

    print(f"✓ Timing data: timing.json")

    # Save metadata
    with open(output_dir / "metadata.json", 'w') as f:
        json.dump(asdict(metadata), f, indent=2)

    print(f"✓ Metadata: metadata.json")

    # Save frame timestamps
    if cap_webcam and cap_webcam.frame_timestamps:
        with open(output_dir / "frame_timestamps.json", 'w') as f:
            json.dump({
                "total_frames": cap_webcam.frame_count,
                "first_frame_time": cap_webcam.first_frame_time,
                "frames": cap_webcam.frame_timestamps
            }, f, indent=2)
        print(f"✓ Frame timestamps: frame_timestamps.json ({cap_webcam.frame_count} frames)")

    # Print summary
    print(f"\n=== Capture Summary ===")
    print(f"Session ID: {session_id}")
    print(f"Output directory: {output_dir}")
    print(f"Duration: {args.duration}s")
    print(f"\nRTL-SDR Left:")
    print(f"  Samples: {cap_left.samples_written:,}")
    print(f"  Size: {cap_left.bytes_written / 1024 / 1024:.1f} MB")
    print(f"  Loss: {timing_data['streams']['rtlsdr_left']['sample_loss_percent']:.2f}%")
    print(f"\nRTL-SDR Right:")
    print(f"  Samples: {cap_right.samples_written:,}")
    print(f"  Size: {cap_right.bytes_written / 1024 / 1024:.1f} MB")
    print(f"  Loss: {timing_data['streams']['rtlsdr_right']['sample_loss_percent']:.2f}%")

    if cap_webcam:
        print(f"\nWebcam: {cap_webcam.frame_count} frames")

    print(f"\nData ready for passive radar processing!")
    print(f"Time synchronization: timing.json")
    print(f"Use sync.py or process.py for analysis")


if __name__ == "__main__":
    main()
