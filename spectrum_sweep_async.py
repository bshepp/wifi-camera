#!/usr/bin/env python3
"""
Asynchronous Spectrum Sweep - Continuous parallel sweeping with all SDRs

Unlike the lockstep spectrum_sweep.py, this runs each SDR independently:
- RTL-SDR x2: Continuously loop through 25 MHz - 1.75 GHz (multiple passes)
- HackRF: Single sweep through 1 MHz - 6 GHz
- Webcam: Continuous capture throughout

Each device logs (timestamp, frequency) pairs for post-capture correlation.
Total sweep time is limited by HackRF's larger range (~7-8 min vs 50+ min lockstep).

Output structure:
  sweep_async_YYYYMMDD_HHMMSS/
    ├── iq_data/
    │   ├── left/       # RTL-SDR left captures
    │   ├── right/      # RTL-SDR right captures  
    │   └── hackrf/     # HackRF captures
    ├── frames/         # Webcam frames
    ├── timing_left.json
    ├── timing_right.json
    ├── timing_hackrf.json
    ├── frame_timestamps.json
    └── metadata.json
"""

import os
import sys
import time
import signal
import subprocess
import threading
import json
import queue
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
import argparse

sys.path.insert(0, str(Path(__file__).parent))
from devices import DeviceManager


@dataclass
class AsyncSweepConfig:
    """Configuration for async spectrum sweep"""
    # RTL-SDR settings
    rtlsdr_freq_start_mhz: float = 25.0
    rtlsdr_freq_end_mhz: float = 1750.0
    rtlsdr_freq_step_mhz: float = 2.0
    rtlsdr_sample_rate: int = 2_560_000
    rtlsdr_samples_per_step: int = 256_000  # 100ms worth
    rtlsdr_settling_ms: int = 20
    rtlsdr_gain: str = "auto"
    
    # HackRF settings  
    hackrf_freq_start_mhz: float = 25.0
    hackrf_freq_end_mhz: float = 6000.0
    hackrf_freq_step_mhz: float = 2.0
    hackrf_sample_rate: int = 8_000_000
    hackrf_samples_per_step: int = 800_000  # 100ms worth
    hackrf_settling_ms: int = 20
    hackrf_lna_gain: int = 16
    hackrf_vga_gain: int = 20
    
    # Control
    max_rtlsdr_passes: int = 0   # Max loops before stopping (0 = unlimited)
    duration_seconds: int = 0    # Run for this many seconds (0 = until HackRF done or max_passes)
    hackrf_continuous: bool = False  # HackRF also loops continuously
    
    @property
    def rtlsdr_frequencies_mhz(self) -> List[float]:
        """RTL-SDR frequency list"""
        freqs = []
        f = self.rtlsdr_freq_start_mhz
        while f <= self.rtlsdr_freq_end_mhz:
            freqs.append(f)
            f += self.rtlsdr_freq_step_mhz
        return freqs
    
    @property
    def hackrf_frequencies_mhz(self) -> List[float]:
        """HackRF frequency list"""
        freqs = []
        f = self.hackrf_freq_start_mhz
        while f <= self.hackrf_freq_end_mhz:
            freqs.append(f)
            f += self.hackrf_freq_step_mhz
        return freqs
    
    @property
    def rtlsdr_steps(self) -> int:
        return len(self.rtlsdr_frequencies_mhz)
    
    @property
    def hackrf_steps(self) -> int:
        return len(self.hackrf_frequencies_mhz)
    
    @property
    def estimated_duration_s(self) -> float:
        """Estimate based on HackRF (slowest device)"""
        time_per_step = (self.hackrf_settling_ms / 1000) + 0.15
        return self.hackrf_steps * time_per_step


@dataclass
class CaptureRecord:
    """Single frequency capture record"""
    frequency_hz: int
    frequency_mhz: float
    timestamp: float
    samples: int
    pass_number: int = 0
    filename: str = ""


class DeviceSweeper:
    """Base class for async device sweeping"""
    
    def __init__(self, name: str, output_dir: Path, stop_event: threading.Event):
        self.name = name
        self.output_dir = output_dir
        self.stop_event = stop_event
        self.captures: List[CaptureRecord] = []
        self.lock = threading.Lock()
        self.pass_count = 0
        self.total_captures = 0
        self.errors = 0
        
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
    def log_capture(self, record: CaptureRecord):
        """Thread-safe capture logging"""
        with self.lock:
            self.captures.append(record)
            self.total_captures += 1
            
    def save_timing(self):
        """Save timing data to JSON"""
        with self.lock:
            timing_data = {
                "device": self.name,
                "total_captures": self.total_captures,
                "passes": self.pass_count,
                "errors": self.errors,
                "captures": [
                    {
                        "frequency_hz": c.frequency_hz,
                        "frequency_mhz": c.frequency_mhz,
                        "timestamp": c.timestamp,
                        "samples": c.samples,
                        "pass": c.pass_number,
                        "filename": c.filename
                    }
                    for c in self.captures
                ]
            }
            
        timing_file = self.output_dir.parent / f"timing_{self.name}.json"
        with open(timing_file, 'w') as f:
            json.dump(timing_data, f, indent=2)
            
        return timing_data


class RTLSDRSweeper(DeviceSweeper):
    """Async RTL-SDR frequency sweeper"""
    
    def __init__(self, name: str, device_index: int, output_dir: Path, 
                 config: AsyncSweepConfig, stop_event: threading.Event):
        super().__init__(name, output_dir, stop_event)
        self.device_index = device_index
        self.config = config
        self.frequencies = config.rtlsdr_frequencies_mhz
        
    def capture_frequency(self, freq_hz: int, freq_mhz: float, 
                         pass_num: int, step_num: int) -> Optional[CaptureRecord]:
        """Capture IQ at a single frequency"""
        bytes_to_capture = self.config.rtlsdr_samples_per_step * 2
        
        # Build filename
        freq_str = f"{int(freq_mhz):05d}"
        filename = f"{self.name}_p{pass_num:02d}_{step_num:04d}_{freq_str}mhz.bin"
        filepath = self.output_dir / filename
        
        cmd = [
            'rtl_sdr',
            '-d', str(self.device_index),
            '-f', str(freq_hz),
            '-s', str(self.config.rtlsdr_sample_rate),
            '-n', str(bytes_to_capture),
            str(filepath)
        ]
        
        if self.config.rtlsdr_gain != "auto":
            cmd.extend(['-g', self.config.rtlsdr_gain])
        
        timestamp = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10.0)
            
            # Check if file was created
            if filepath.exists():
                samples = filepath.stat().st_size // 2
                return CaptureRecord(
                    frequency_hz=freq_hz,
                    frequency_mhz=freq_mhz,
                    timestamp=timestamp,
                    samples=samples,
                    pass_number=pass_num,
                    filename=filename
                )
            else:
                self.errors += 1
                return None
                
        except subprocess.TimeoutExpired:
            self.errors += 1
            return None
        except Exception as e:
            self.errors += 1
            return None
    
    def run(self, progress_callback=None):
        """Run continuous sweep until stopped"""
        max_passes = self.config.max_rtlsdr_passes
        
        while not self.stop_event.is_set():
            self.pass_count += 1
            
            if max_passes > 0 and self.pass_count > max_passes:
                break
                
            for i, freq_mhz in enumerate(self.frequencies):
                if self.stop_event.is_set():
                    break
                    
                freq_hz = int(freq_mhz * 1e6)
                
                record = self.capture_frequency(freq_hz, freq_mhz, self.pass_count, i)
                
                if record:
                    self.log_capture(record)
                    
                if progress_callback:
                    progress_callback(self.name, self.pass_count, i + 1, 
                                    len(self.frequencies), freq_mhz)
                
                # Settling time
                time.sleep(self.config.rtlsdr_settling_ms / 1000)
                
        return self.save_timing()


class HackRFSweeper(DeviceSweeper):
    """Async HackRF frequency sweeper"""
    
    def __init__(self, output_dir: Path, config: AsyncSweepConfig, 
                 stop_event: threading.Event, done_event: threading.Event,
                 continuous: bool = False):
        super().__init__("hackrf", output_dir, stop_event)
        self.config = config
        self.frequencies = config.hackrf_frequencies_mhz
        self.done_event = done_event  # Signal when HackRF completes (if not continuous)
        self.continuous = continuous  # Loop continuously like RTL-SDRs
        
    def capture_frequency(self, freq_hz: int, freq_mhz: float, 
                         step_num: int) -> Optional[CaptureRecord]:
        """Capture IQ at a single frequency"""
        # Build filename
        freq_str = f"{int(freq_mhz):05d}"
        filename = f"hackrf_{step_num:04d}_{freq_str}mhz.bin"
        filepath = self.output_dir / filename
        
        cmd = [
            'hackrf_transfer',
            '-r', str(filepath),
            '-f', str(freq_hz),
            '-s', str(self.config.hackrf_sample_rate),
            '-l', str(self.config.hackrf_lna_gain),
            '-g', str(self.config.hackrf_vga_gain),
            '-n', str(self.config.hackrf_samples_per_step * 2)
        ]
        
        timestamp = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10.0)
            
            if filepath.exists():
                samples = filepath.stat().st_size // 2
                return CaptureRecord(
                    frequency_hz=freq_hz,
                    frequency_mhz=freq_mhz,
                    timestamp=timestamp,
                    samples=samples,
                    pass_number=1,
                    filename=filename
                )
            else:
                self.errors += 1
                return None
                
        except subprocess.TimeoutExpired:
            self.errors += 1
            return None
        except Exception as e:
            self.errors += 1
            return None
    
    def run(self, progress_callback=None):
        """Run sweep through full range (single or continuous)"""
        
        while not self.stop_event.is_set():
            self.pass_count += 1
            
            for i, freq_mhz in enumerate(self.frequencies):
                if self.stop_event.is_set():
                    break
                    
                freq_hz = int(freq_mhz * 1e6)
                
                record = self.capture_frequency(freq_hz, freq_mhz, i)
                if record:
                    record.pass_number = self.pass_count
                    self.log_capture(record)
                    
                if progress_callback:
                    progress_callback("hackrf", self.pass_count, i + 1, 
                                    len(self.frequencies), freq_mhz)
                
                # Settling time
                time.sleep(self.config.hackrf_settling_ms / 1000)
            
            # If not continuous mode, exit after first sweep
            if not self.continuous:
                break
        
        # Signal completion (only matters in non-continuous mode)
        self.done_event.set()
        
        return self.save_timing()


class AsyncSweepCapture:
    """Coordinated async sweep across all devices"""
    
    def __init__(self, output_dir: Path, config: AsyncSweepConfig):
        self.output_dir = output_dir
        self.config = config
        self.dev_mgr = DeviceManager()
        
        # Devices
        self.rtlsdr_left = None
        self.rtlsdr_right = None
        self.hackrf = None
        self.webcam = None
        
        # Threading control
        self.stop_event = threading.Event()
        self.hackrf_done_event = threading.Event()
        
        # Sweepers
        self.sweepers: Dict[str, DeviceSweeper] = {}
        
        # Webcam
        self.webcam_process = None
        self.webcam_start_time = 0
        self.frame_count = 0
        self.frame_timestamps: List[Dict] = []
        self.frames_dir = output_dir / "frames"
        
        # Progress tracking
        self.progress_lock = threading.Lock()
        self.progress = {}
        
    def detect_devices(self) -> bool:
        """Detect all available devices"""
        rtlsdr_devices = self.dev_mgr.detect_rtlsdr()
        
        if len(rtlsdr_devices) >= 2:
            self.rtlsdr_left = next((d for d in rtlsdr_devices if d.position == "left"), None)
            self.rtlsdr_right = next((d for d in rtlsdr_devices if d.position == "right"), None)
        elif len(rtlsdr_devices) == 1:
            self.rtlsdr_left = rtlsdr_devices[0]
            
        if self.rtlsdr_left:
            print(f"✓ RTL-SDR Left: index {self.rtlsdr_left.index}")
        if self.rtlsdr_right:
            print(f"✓ RTL-SDR Right: index {self.rtlsdr_right.index}")
            
        self.hackrf = self.dev_mgr.detect_hackrf()
        if self.hackrf:
            print(f"✓ HackRF: {self.hackrf.serial}")
        else:
            print("⚠ HackRF not found - sweep will be RTL-SDR only")
            
        self.webcam = self.dev_mgr.detect_webcam()
        if self.webcam:
            print(f"✓ Webcam: {self.webcam.device_path}")
        else:
            print("⚠ No webcam detected")
            
        return self.rtlsdr_left is not None or self.hackrf is not None
    
    def start_webcam(self, duration: int):
        """Start webcam capture"""
        if not self.webcam:
            return
            
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        
        cmd = [
            'ffmpeg', '-y',
            '-f', 'v4l2',
            '-framerate', '30',
            '-video_size', '1280x720',
            '-i', self.webcam.device_path,
            '-t', str(duration + 30),
            '-q:v', '2',
            f'{self.frames_dir}/frame_%06d.jpg'
        ]
        
        self.webcam_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        self.webcam_start_time = time.time()
        print(f"✓ Webcam started")
        
    def stop_webcam(self):
        """Stop webcam and build timestamp list"""
        if self.webcam_process:
            self.webcam_process.terminate()
            try:
                self.webcam_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.webcam_process.kill()
                
        if self.frames_dir.exists():
            frame_files = sorted(self.frames_dir.glob("frame_*.jpg"))
            self.frame_count = len(frame_files)
            
            if self.frame_count > 0:
                frame_interval = 1.0 / 30.0
                for i, frame_file in enumerate(frame_files):
                    timestamp = self.webcam_start_time + (i * frame_interval)
                    self.frame_timestamps.append({
                        "frame": i,
                        "filename": frame_file.name,
                        "timestamp": timestamp
                    })
                    
    def progress_callback(self, device: str, pass_num: int, step: int, 
                         total: int, freq_mhz: float):
        """Update progress display"""
        with self.progress_lock:
            self.progress[device] = {
                "pass": pass_num,
                "step": step,
                "total": total,
                "freq": freq_mhz,
                "pct": (step / total) * 100
            }
            
    def display_progress(self, start_time: float):
        """Background thread to display progress"""
        duration = self.config.duration_seconds
        
        while not self.stop_event.is_set() and not self.hackrf_done_event.is_set():
            elapsed = time.time() - start_time
            
            with self.progress_lock:
                parts = []
                
                # Time display
                if duration > 0:
                    remaining = max(0, duration - elapsed)
                    parts.append(f"T:{int(elapsed)}s/{duration}s")
                else:
                    parts.append(f"T:{int(elapsed)}s")
                
                for name in ['left', 'right', 'hackrf']:
                    if name in self.progress:
                        p = self.progress[name]
                        if name == 'hackrf':
                            if self.config.hackrf_continuous:
                                parts.append(f"H:p{p['pass']} {p['freq']:5.0f}MHz")
                            else:
                                parts.append(f"H:{p['freq']:5.0f}MHz {p['pct']:4.1f}%")
                        else:
                            n = 'L' if name == 'left' else 'R'
                            parts.append(f"{n}:p{p['pass']} {p['freq']:5.0f}MHz")
                            
                if parts:
                    line = " | ".join(parts)
                    print(f"\r{line}    ", end='', flush=True)
                    
            time.sleep(0.5)
            
    def run_sweep(self) -> bool:
        """Execute async sweep"""
        print(f"\n=== Starting Async Spectrum Sweep ===")
        print(f"RTL-SDR range: {self.config.rtlsdr_freq_start_mhz} - {self.config.rtlsdr_freq_end_mhz} MHz ({self.config.rtlsdr_steps} steps)")
        print(f"HackRF range:  {self.config.hackrf_freq_start_mhz} - {self.config.hackrf_freq_end_mhz} MHz ({self.config.hackrf_steps} steps)")
        print(f"Step size: {self.config.rtlsdr_freq_step_mhz} MHz")
        print(f"Estimated duration: {self.config.estimated_duration_s:.1f}s ({self.config.estimated_duration_s/60:.1f} min)")
        print()
        
        # Create output directories
        iq_base = self.output_dir / "iq_data"
        
        threads = []
        sweep_start = time.time()
        
        # Start webcam
        self.start_webcam(int(self.config.estimated_duration_s) + 60)
        time.sleep(1)
        
        # Create and start HackRF sweeper (if available)
        if self.hackrf:
            hackrf_dir = iq_base / "hackrf"
            hackrf_sweeper = HackRFSweeper(
                hackrf_dir, self.config, 
                self.stop_event, self.hackrf_done_event,
                continuous=self.config.hackrf_continuous
            )
            self.sweepers['hackrf'] = hackrf_sweeper
            
            t = threading.Thread(
                target=hackrf_sweeper.run,
                args=(self.progress_callback,),
                name="hackrf_sweep"
            )
            threads.append(t)
            
        # Create and start RTL-SDR sweepers
        if self.rtlsdr_left:
            left_dir = iq_base / "left"
            left_sweeper = RTLSDRSweeper(
                "left", self.rtlsdr_left.index, left_dir,
                self.config, self.stop_event
            )
            self.sweepers['left'] = left_sweeper
            
            t = threading.Thread(
                target=left_sweeper.run,
                args=(self.progress_callback,),
                name="left_sweep"
            )
            threads.append(t)
            
        if self.rtlsdr_right:
            right_dir = iq_base / "right"
            right_sweeper = RTLSDRSweeper(
                "right", self.rtlsdr_right.index, right_dir,
                self.config, self.stop_event
            )
            self.sweepers['right'] = right_sweeper
            
            t = threading.Thread(
                target=right_sweeper.run,
                args=(self.progress_callback,),
                name="right_sweep"
            )
            threads.append(t)
            
        # Start progress display
        progress_thread = threading.Thread(
            target=self.display_progress,
            args=(sweep_start,),
            name="progress_display",
            daemon=True
        )
        progress_thread.start()
        
        # Launch all sweep threads
        print("Starting device sweeps...")
        for t in threads:
            t.start()
        
        # Determine stop condition
        duration = self.config.duration_seconds
        use_duration = duration > 0
        
        if use_duration:
            print(f"Running for {duration}s ({duration/60:.1f} min)...")
        elif self.config.hackrf_continuous:
            print("Running continuously (Ctrl+C to stop)...")
        else:
            print("Running until HackRF completes...")
            
        # Wait for completion
        try:
            if use_duration:
                # Duration-based: wait for timer
                end_time = sweep_start + duration
                while time.time() < end_time and not self.stop_event.is_set():
                    remaining = end_time - time.time()
                    time.sleep(min(1.0, remaining))
                    
                print(f"\n\nDuration reached ({duration}s), stopping...")
                self.stop_event.set()
                
            elif self.hackrf and not self.config.hackrf_continuous:
                # Wait for HackRF completion (original behavior)
                while not self.hackrf_done_event.is_set() and not self.stop_event.is_set():
                    self.hackrf_done_event.wait(timeout=1.0)
                    
                if not self.stop_event.is_set():
                    print("\n\nHackRF sweep complete, stopping RTL-SDRs...")
                    self.stop_event.set()
            else:
                # Continuous mode with no duration - wait for Ctrl+C
                while not self.stop_event.is_set():
                    time.sleep(1.0)
                    
        except KeyboardInterrupt:
            print("\n\nInterrupt received, stopping...")
            self.stop_event.set()
            
        # Wait for all threads to finish
        for t in threads:
            t.join(timeout=10.0)
            
        sweep_end = time.time()
        
        # Stop webcam
        self.stop_webcam()
        
        print(f"\nSweep complete in {sweep_end - sweep_start:.1f}s")
        
        return True
    
    def save_metadata(self):
        """Save all metadata"""
        # Collect stats from sweepers
        device_stats = {}
        for name, sweeper in self.sweepers.items():
            device_stats[name] = {
                "total_captures": sweeper.total_captures,
                "passes": sweeper.pass_count,
                "errors": sweeper.errors
            }
            
        metadata = {
            "session_id": self.output_dir.name,
            "sweep_type": "async",
            "start_time": self.webcam_start_time,
            "config": {
                "rtlsdr_freq_start_mhz": self.config.rtlsdr_freq_start_mhz,
                "rtlsdr_freq_end_mhz": self.config.rtlsdr_freq_end_mhz,
                "rtlsdr_freq_step_mhz": self.config.rtlsdr_freq_step_mhz,
                "rtlsdr_sample_rate": self.config.rtlsdr_sample_rate,
                "rtlsdr_samples_per_step": self.config.rtlsdr_samples_per_step,
                "hackrf_freq_start_mhz": self.config.hackrf_freq_start_mhz,
                "hackrf_freq_end_mhz": self.config.hackrf_freq_end_mhz,
                "hackrf_freq_step_mhz": self.config.hackrf_freq_step_mhz,
                "hackrf_sample_rate": self.config.hackrf_sample_rate,
                "hackrf_samples_per_step": self.config.hackrf_samples_per_step,
            },
            "devices": {
                "rtlsdr_left_index": self.rtlsdr_left.index if self.rtlsdr_left else None,
                "rtlsdr_right_index": self.rtlsdr_right.index if self.rtlsdr_right else None,
                "hackrf_serial": self.hackrf.serial if self.hackrf else None,
                "webcam": self.webcam.device_path if self.webcam else None
            },
            "stats": device_stats,
            "total_frames": self.frame_count
        }
        
        with open(self.output_dir / "metadata.json", 'w') as f:
            json.dump(metadata, f, indent=2)
            
        # Frame timestamps
        if self.frame_timestamps:
            with open(self.output_dir / "frame_timestamps.json", 'w') as f:
                json.dump({
                    "total_frames": self.frame_count,
                    "webcam_start_time": self.webcam_start_time,
                    "frames": self.frame_timestamps
                }, f, indent=2)
                
        print(f"✓ Saved metadata.json")
        print(f"✓ Saved timing files for each device")
        if self.frame_timestamps:
            print(f"✓ Saved frame_timestamps.json ({self.frame_count} frames)")


def main():
    parser = argparse.ArgumentParser(
        description='Async Spectrum Sweep - Continuous parallel SDR sweeping'
    )
    
    # RTL-SDR options
    parser.add_argument('--rtl-start', type=float, default=25.0,
                        help='RTL-SDR start frequency MHz (default: 25)')
    parser.add_argument('--rtl-end', type=float, default=1750.0,
                        help='RTL-SDR end frequency MHz (default: 1750)')
    parser.add_argument('--rtl-step', type=float, default=2.0,
                        help='RTL-SDR step size MHz (default: 2.0)')
    parser.add_argument('--rtl-samples', type=int, default=256000,
                        help='RTL-SDR samples per step (default: 256000)')
    parser.add_argument('--rtl-gain', type=str, default="auto",
                        help='RTL-SDR gain (default: auto)')
    
    # HackRF options
    parser.add_argument('--hackrf-start', type=float, default=25.0,
                        help='HackRF start frequency MHz (default: 25)')
    parser.add_argument('--hackrf-end', type=float, default=6000.0,
                        help='HackRF end frequency MHz (default: 6000)')
    parser.add_argument('--hackrf-step', type=float, default=2.0,
                        help='HackRF step size MHz (default: 2.0)')
    parser.add_argument('--hackrf-samples', type=int, default=800000,
                        help='HackRF samples per step (default: 800000)')
    
    # Control options
    parser.add_argument('--duration', '-t', type=int, default=0,
                        help='Run for N seconds (0 = until HackRF done or continuous)')
    parser.add_argument('--continuous', '-c', action='store_true',
                        help='All devices sweep continuously (requires --duration or Ctrl+C)')
    parser.add_argument('--max-passes', type=int, default=0,
                        help='Max RTL-SDR passes (0 = unlimited)')
    parser.add_argument('--settling', type=int, default=20,
                        help='Settling time ms (default: 20)')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Output directory')
    
    # Presets
    parser.add_argument('--quick', action='store_true',
                        help='Quick test: 100-500 MHz, 10 MHz steps, 60s duration')
    parser.add_argument('--full', action='store_true',
                        help='Full async sweep (HackRF single pass)')
    
    args = parser.parse_args()
    
    # Apply presets
    if args.quick:
        args.rtl_start = 100.0
        args.rtl_end = 500.0
        args.rtl_step = 10.0
        args.hackrf_start = 100.0
        args.hackrf_end = 1000.0
        args.hackrf_step = 10.0
        args.rtl_samples = 128000
        args.hackrf_samples = 400000
        if args.duration == 0:
            args.duration = 60  # 1 minute default for quick test
        
    # Build config
    config = AsyncSweepConfig(
        rtlsdr_freq_start_mhz=args.rtl_start,
        rtlsdr_freq_end_mhz=args.rtl_end,
        rtlsdr_freq_step_mhz=args.rtl_step,
        rtlsdr_samples_per_step=args.rtl_samples,
        rtlsdr_gain=args.rtl_gain,
        rtlsdr_settling_ms=args.settling,
        hackrf_freq_start_mhz=args.hackrf_start,
        hackrf_freq_end_mhz=args.hackrf_end,
        hackrf_freq_step_mhz=args.hackrf_step,
        hackrf_samples_per_step=args.hackrf_samples,
        hackrf_settling_ms=args.settling,
        max_rtlsdr_passes=args.max_passes,
        duration_seconds=args.duration,
        hackrf_continuous=args.continuous
    )
    
    # Create output directory
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = Path(__file__).parent / "data" / f"sweep_async_{session_id}"
        
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"=== Async Spectrum Sweep ===")
    print(f"Session: {session_id}")
    print(f"Output: {output_dir}")
    print()
    
    # Create sweep manager
    sweep = AsyncSweepCapture(output_dir, config)
    
    # Detect devices
    print("Detecting devices...")
    if not sweep.detect_devices():
        print("ERROR: No usable SDR devices found!")
        sys.exit(1)
        
    print()
    
    # Setup signal handler
    def signal_handler(sig, frame):
        print("\n\nInterrupt received...")
        sweep.stop_event.set()
        
    signal.signal(signal.SIGINT, signal_handler)
    
    # Run sweep
    sweep.run_sweep()
    
    # Save metadata
    sweep.save_metadata()
    
    # Summary
    print(f"\n=== Sweep Summary ===")
    for name, sweeper in sweep.sweepers.items():
        print(f"{name}: {sweeper.total_captures} captures over {sweeper.pass_count} pass(es), {sweeper.errors} errors")
        
    print(f"Webcam: {sweep.frame_count} frames")
    
    # Calculate data size
    iq_dir = output_dir / "iq_data"
    if iq_dir.exists():
        total_bytes = sum(f.stat().st_size for f in iq_dir.rglob("*.bin"))
        print(f"\nTotal IQ data: {total_bytes / 1024 / 1024:.1f} MB")
        
    print(f"\nOutput: {output_dir}")


if __name__ == "__main__":
    main()

