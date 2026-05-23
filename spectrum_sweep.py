#!/usr/bin/env python3
"""
Synchronized Spectrum Sweep - Full band capture with dual RTL-SDR + HackRF + webcam

Sweeps all three SDRs across their frequency ranges simultaneously:
- RTL-SDR x2: 25 MHz - 1.75 GHz  
- HackRF: 1 MHz - 6 GHz

Output:
- Per-frequency IQ snapshots from all three SDRs
- Timestamped video frames
- Frequency-time mapping for correlation
"""

import os
import sys
import time
import signal
import subprocess
import threading
import json
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Tuple
import argparse

sys.path.insert(0, str(Path(__file__).parent))
from devices import DeviceManager


@dataclass
class SweepConfig:
    """Configuration for spectrum sweep"""
    freq_start_mhz: float = 25.0       # Start frequency (MHz)
    freq_end_mhz: float = 6000.0       # End frequency (MHz) - HackRF max
    freq_step_mhz: float = 2.0         # Step size (MHz)
    
    # RTL-SDR settings (2.56 MSPS recommended stable max)
    rtlsdr_sample_rate: int = 2_560_000
    rtlsdr_samples_per_step: int = 256_000  # 100ms worth
    rtlsdr_max_freq_mhz: float = 1750.0     # RTL-SDR max frequency
    
    # HackRF settings (8 MSPS recommended for USB stability)
    hackrf_sample_rate: int = 8_000_000
    hackrf_samples_per_step: int = 800_000  # 100ms worth
    
    settling_time_ms: int = 25         # Time to wait after frequency change
    gain: str = "auto"                 # RTL-SDR gain setting
    hackrf_lna_gain: int = 16          # HackRF LNA gain
    hackrf_vga_gain: int = 20          # HackRF VGA gain
    
    @property
    def frequencies_mhz(self) -> List[float]:
        """Generate list of frequencies to sweep"""
        freqs = []
        f = self.freq_start_mhz
        while f <= self.freq_end_mhz:
            freqs.append(f)
            f += self.freq_step_mhz
        return freqs
    
    @property
    def num_steps(self) -> int:
        return len(self.frequencies_mhz)
    
    @property
    def estimated_duration_s(self) -> float:
        """Estimate total sweep time"""
        time_per_step = (self.settling_time_ms / 1000) + 0.15  # ~150ms per step
        return self.num_steps * time_per_step


@dataclass  
class FrequencyCapture:
    """Data captured at a single frequency"""
    frequency_hz: int
    frequency_mhz: float
    timestamp_start: float
    timestamp_end: float
    samples_left: int = 0
    samples_right: int = 0
    samples_hackrf: int = 0
    rtlsdr_in_range: bool = True
    frame_indices: List[int] = field(default_factory=list)


class SyncedSweepCapture:
    """Synchronized spectrum sweep across dual RTL-SDRs + HackRF"""
    
    def __init__(self, output_dir: Path, config: SweepConfig):
        self.output_dir = output_dir
        self.config = config
        self.dev_mgr = DeviceManager()
        
        # Device info
        self.rtlsdr_left = None
        self.rtlsdr_right = None
        self.hackrf = None
        self.webcam = None
        
        # Capture state
        self.running = False
        self.captures: List[FrequencyCapture] = []
        self.frame_timestamps: List[Dict] = []
        self.frame_count = 0
        self.webcam_start_time = 0
        
        # Webcam process
        self.webcam_process = None
        self.frames_dir = output_dir / "frames"
        
    def detect_devices(self) -> bool:
        """Detect and validate required devices"""
        rtlsdr_devices = self.dev_mgr.detect_rtlsdr()
        
        if len(rtlsdr_devices) < 2:
            print(f"⚠ Found {len(rtlsdr_devices)} RTL-SDR devices (need 2 for full coverage)")
        else:
            self.rtlsdr_left = next((d for d in rtlsdr_devices if d.position == "left"), None)
            self.rtlsdr_right = next((d for d in rtlsdr_devices if d.position == "right"), None)
            
            if self.rtlsdr_left:
                print(f"✓ RTL-SDR Left: index {self.rtlsdr_left.index}")
            if self.rtlsdr_right:
                print(f"✓ RTL-SDR Right: index {self.rtlsdr_right.index}")
                
        self.hackrf = self.dev_mgr.detect_hackrf()
        if self.hackrf:
            print(f"✓ HackRF: {self.hackrf.serial}")
        else:
            print("⚠ HackRF not found")
            
        self.webcam = self.dev_mgr.detect_webcam()
        if self.webcam:
            print(f"✓ Webcam: {self.webcam.device_path}")
        else:
            print("⚠ No webcam detected")
        
        # Need at least one SDR
        if not self.rtlsdr_left and not self.rtlsdr_right and not self.hackrf:
            print("ERROR: No SDR devices found!")
            return False
            
        return True
    
    def start_webcam(self, duration: int):
        """Start webcam capture in background"""
        if not self.webcam:
            return
            
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        
        cmd = [
            'ffmpeg', '-y',
            '-f', 'v4l2',
            '-framerate', '30',
            '-video_size', '1280x720',
            '-i', self.webcam.device_path,
            '-t', str(duration + 10),  # Extra buffer
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
        """Stop webcam and generate timestamps"""
        if self.webcam_process:
            self.webcam_process.terminate()
            try:
                self.webcam_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.webcam_process.kill()
                
        # Generate frame timestamps
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
    
    def capture_rtlsdr(self, device_index: int, freq_hz: int, name: str) -> Tuple[bytes, float]:
        """Capture IQ from a single RTL-SDR"""
        # rtl_sdr's -n counts IQ pairs (2 bytes each on disk / stdout).
        cmd = [
            'rtl_sdr',
            '-d', str(device_index),
            '-f', str(freq_hz),
            '-s', str(self.config.rtlsdr_sample_rate),
            '-g', self.config.gain,
            '-n', str(self.config.rtlsdr_samples_per_step),
            '-'
        ]
        
        start_time = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10.0)
            return result.stdout, start_time
        except subprocess.TimeoutExpired:
            return b'', start_time
        except Exception as e:
            print(f"\n  RTL-SDR {name} error: {e}")
            return b'', start_time
    
    def capture_hackrf(self, freq_hz: int) -> Tuple[bytes, float]:
        """Capture IQ from HackRF"""
        # HackRF needs to write to a temp file
        temp_file = self.output_dir / "temp_hackrf.bin"
        
        # Calculate duration for desired samples
        duration_ms = int((self.config.hackrf_samples_per_step / self.config.hackrf_sample_rate) * 1000) + 50
        
        # hackrf_transfer's -n counts IQ pairs (2 bytes each on disk).
        cmd = [
            'hackrf_transfer',
            '-r', str(temp_file),
            '-f', str(freq_hz),
            '-s', str(self.config.hackrf_sample_rate),
            '-l', str(self.config.hackrf_lna_gain),
            '-g', str(self.config.hackrf_vga_gain),
            '-n', str(self.config.hackrf_samples_per_step)
        ]
        
        start_time = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10.0)
            
            # Read the captured data
            if temp_file.exists():
                with open(temp_file, 'rb') as f:
                    data = f.read()
                temp_file.unlink()  # Clean up
                return data, start_time
            return b'', start_time
            
        except subprocess.TimeoutExpired:
            if temp_file.exists():
                temp_file.unlink()
            return b'', start_time
        except Exception as e:
            print(f"\n  HackRF error: {e}")
            if temp_file.exists():
                temp_file.unlink()
            return b'', start_time
    
    def capture_at_frequency(self, freq_hz: int, freq_mhz: float, step_num: int) -> FrequencyCapture:
        """Capture IQ data from all SDRs at a specific frequency"""
        
        rtlsdr_in_range = freq_mhz <= self.config.rtlsdr_max_freq_mhz
        
        # Use threading to capture all devices simultaneously
        results = {}
        threads = []
        lock = threading.Lock()
        barrier_count = 0
        
        # Count active devices for this frequency
        if self.hackrf:
            barrier_count += 1
        if rtlsdr_in_range:
            if self.rtlsdr_left:
                barrier_count += 1
            if self.rtlsdr_right:
                barrier_count += 1
                
        if barrier_count == 0:
            # No devices can capture this frequency
            return FrequencyCapture(
                frequency_hz=freq_hz,
                frequency_mhz=freq_mhz,
                timestamp_start=time.time(),
                timestamp_end=time.time(),
                rtlsdr_in_range=rtlsdr_in_range
            )
        
        barrier = threading.Barrier(barrier_count)
        
        def capture_device(name: str, capture_func, *args):
            try:
                barrier.wait(timeout=5.0)
            except threading.BrokenBarrierError:
                pass
            data, ts = capture_func(*args)
            with lock:
                results[name] = {'data': data, 'timestamp': ts}
        
        # Start capture threads
        if self.hackrf:
            t = threading.Thread(target=capture_device, 
                               args=('hackrf', self.capture_hackrf, freq_hz))
            threads.append(t)
            
        if rtlsdr_in_range:
            if self.rtlsdr_left:
                t = threading.Thread(target=capture_device,
                                   args=('left', self.capture_rtlsdr, 
                                         self.rtlsdr_left.index, freq_hz, 'left'))
                threads.append(t)
                
            if self.rtlsdr_right:
                t = threading.Thread(target=capture_device,
                                   args=('right', self.capture_rtlsdr,
                                         self.rtlsdr_right.index, freq_hz, 'right'))
                threads.append(t)
        
        # Launch all
        for t in threads:
            t.start()
            
        # Wait for completion
        for t in threads:
            t.join(timeout=15.0)
        
        # Settling time
        time.sleep(self.config.settling_time_ms / 1000)
        
        # Build capture record
        timestamps = [r['timestamp'] for r in results.values() if r.get('timestamp')]
        
        capture = FrequencyCapture(
            frequency_hz=freq_hz,
            frequency_mhz=freq_mhz,
            timestamp_start=min(timestamps) if timestamps else time.time(),
            timestamp_end=time.time(),
            samples_left=len(results.get('left', {}).get('data', b'')) // 2,
            samples_right=len(results.get('right', {}).get('data', b'')) // 2,
            samples_hackrf=len(results.get('hackrf', {}).get('data', b'')) // 2,
            rtlsdr_in_range=rtlsdr_in_range
        )
        
        return capture, results
    
    def run_sweep(self) -> bool:
        """Execute the full spectrum sweep"""
        
        frequencies = self.config.frequencies_mhz
        total_steps = len(frequencies)
        
        # Count how many are in RTL-SDR range
        rtlsdr_steps = sum(1 for f in frequencies if f <= self.config.rtlsdr_max_freq_mhz)
        hackrf_only_steps = total_steps - rtlsdr_steps
        
        print(f"\n=== Starting Spectrum Sweep ===")
        print(f"Range: {self.config.freq_start_mhz} - {self.config.freq_end_mhz} MHz")
        print(f"Total steps: {total_steps} ({self.config.freq_step_mhz} MHz each)")
        print(f"  RTL-SDR range (≤{self.config.rtlsdr_max_freq_mhz} MHz): {rtlsdr_steps} steps")
        print(f"  HackRF only (>{self.config.rtlsdr_max_freq_mhz} MHz): {hackrf_only_steps} steps")
        print(f"Estimated duration: {self.config.estimated_duration_s:.1f}s ({self.config.estimated_duration_s/60:.1f} min)")
        print()
        
        # Create output directories
        iq_dir = self.output_dir / "iq_data"
        iq_dir.mkdir(parents=True, exist_ok=True)
        
        # Start webcam
        self.start_webcam(int(self.config.estimated_duration_s) + 30)
        time.sleep(1)
        
        self.running = True
        sweep_start = time.time()
        
        try:
            for i, freq_mhz in enumerate(frequencies):
                if not self.running:
                    break
                    
                freq_hz = int(freq_mhz * 1e6)
                
                # Progress display
                progress = ((i + 1) / total_steps) * 100
                elapsed = time.time() - sweep_start
                eta = (elapsed / (i + 1)) * (total_steps - i - 1) if i > 0 else 0
                
                # Show which devices are active
                devices = "H"  # HackRF always
                if freq_mhz <= self.config.rtlsdr_max_freq_mhz:
                    devices = "LRH"
                    
                print(f"\r[{progress:5.1f}%] {freq_mhz:7.1f} MHz [{devices}] "
                      f"({i+1}/{total_steps})  "
                      f"Elapsed: {elapsed:5.1f}s  ETA: {eta:5.1f}s   ", end='', flush=True)
                
                # Capture at this frequency
                capture, results = self.capture_at_frequency(freq_hz, freq_mhz, i)
                
                # Save IQ data
                freq_str = f"{int(freq_mhz):05d}"
                
                if 'left' in results and results['left']['data']:
                    with open(iq_dir / f"left_{i:04d}_{freq_str}mhz.bin", 'wb') as f:
                        f.write(results['left']['data'])
                        
                if 'right' in results and results['right']['data']:
                    with open(iq_dir / f"right_{i:04d}_{freq_str}mhz.bin", 'wb') as f:
                        f.write(results['right']['data'])
                        
                if 'hackrf' in results and results['hackrf']['data']:
                    with open(iq_dir / f"hackrf_{i:04d}_{freq_str}mhz.bin", 'wb') as f:
                        f.write(results['hackrf']['data'])
                
                self.captures.append(capture)
                
        except KeyboardInterrupt:
            print("\n\nSweep interrupted!")
            self.running = False
            
        sweep_end = time.time()
        print(f"\n\nSweep complete in {sweep_end - sweep_start:.1f}s")
        
        # Stop webcam
        self.stop_webcam()
        
        return True
    
    def save_metadata(self):
        """Save sweep metadata and timing info"""
        
        metadata = {
            "session_id": self.output_dir.name,
            "sweep_start_time": self.captures[0].timestamp_start if self.captures else None,
            "sweep_end_time": self.captures[-1].timestamp_end if self.captures else None,
            "config": {
                "freq_start_mhz": self.config.freq_start_mhz,
                "freq_end_mhz": self.config.freq_end_mhz,
                "freq_step_mhz": self.config.freq_step_mhz,
                "rtlsdr_sample_rate": self.config.rtlsdr_sample_rate,
                "rtlsdr_samples_per_step": self.config.rtlsdr_samples_per_step,
                "rtlsdr_max_freq_mhz": self.config.rtlsdr_max_freq_mhz,
                "hackrf_sample_rate": self.config.hackrf_sample_rate,
                "hackrf_samples_per_step": self.config.hackrf_samples_per_step,
                "settling_time_ms": self.config.settling_time_ms
            },
            "devices": {
                "rtlsdr_left_index": self.rtlsdr_left.index if self.rtlsdr_left else None,
                "rtlsdr_right_index": self.rtlsdr_right.index if self.rtlsdr_right else None,
                "hackrf_serial": self.hackrf.serial if self.hackrf else None,
                "webcam": self.webcam.device_path if self.webcam else None
            },
            "total_frequencies": len(self.captures),
            "total_frames": self.frame_count
        }
        
        with open(self.output_dir / "metadata.json", 'w') as f:
            json.dump(metadata, f, indent=2)
            
        # Per-frequency timing data  
        freq_data = []
        for cap in self.captures:
            freq_data.append({
                "frequency_hz": cap.frequency_hz,
                "frequency_mhz": cap.frequency_mhz,
                "timestamp_start": cap.timestamp_start,
                "timestamp_end": cap.timestamp_end,
                "samples_left": cap.samples_left,
                "samples_right": cap.samples_right,
                "samples_hackrf": cap.samples_hackrf,
                "rtlsdr_in_range": cap.rtlsdr_in_range,
                "frame_indices": cap.frame_indices
            })
            
        with open(self.output_dir / "frequency_timing.json", 'w') as f:
            json.dump(freq_data, f, indent=2)
            
        # Frame timestamps
        if self.frame_timestamps:
            with open(self.output_dir / "frame_timestamps.json", 'w') as f:
                json.dump({
                    "total_frames": self.frame_count,
                    "webcam_start_time": self.webcam_start_time,
                    "frames": self.frame_timestamps
                }, f, indent=2)
                
        print(f"✓ Saved metadata.json")
        print(f"✓ Saved frequency_timing.json ({len(freq_data)} entries)")
        if self.frame_timestamps:
            print(f"✓ Saved frame_timestamps.json ({self.frame_count} frames)")


def main():
    parser = argparse.ArgumentParser(description='Synchronized Spectrum Sweep (RTL-SDR + HackRF)')
    parser.add_argument('--start', type=float, default=25.0, 
                        help='Start frequency in MHz (default: 25)')
    parser.add_argument('--end', type=float, default=6000.0,
                        help='End frequency in MHz (default: 6000)')
    parser.add_argument('--step', type=float, default=2.0,
                        help='Frequency step in MHz (default: 2.0)')
    parser.add_argument('--rtl-samples', type=int, default=240000,
                        help='RTL-SDR samples per step (default: 240000 = 100ms)')
    parser.add_argument('--hackrf-samples', type=int, default=1000000,
                        help='HackRF samples per step (default: 1000000 = 100ms)')
    parser.add_argument('--settling', type=int, default=25,
                        help='Settling time in ms (default: 25)')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Output directory')
    parser.add_argument('--quick', action='store_true',
                        help='Quick test: 100-500 MHz, 10 MHz steps')
    parser.add_argument('--wifi', action='store_true',
                        help='WiFi bands only: 2.4 GHz and 5 GHz')
    parser.add_argument('--full', action='store_true',
                        help='Full sweep: 25 MHz - 6 GHz (takes ~50 minutes)')
    parser.add_argument('--confirm', action='store_true',
                        help='Wait for confirmation before starting')
    args = parser.parse_args()
    
    # Preset modes
    if args.quick:
        args.start = 100.0
        args.end = 500.0
        args.step = 10.0
        args.rtl_samples = 120000
        args.hackrf_samples = 500000
        
    elif args.wifi:
        # WiFi bands - will need multiple sweeps
        # 2.4 GHz: 2400-2500 MHz
        # 5 GHz: 5150-5850 MHz
        args.start = 2400.0
        args.end = 2500.0  # Just 2.4 GHz for now
        args.step = 1.0
        
    elif args.full:
        args.start = 25.0
        args.end = 6000.0
        args.step = 2.0
        
    # Create config
    config = SweepConfig(
        freq_start_mhz=args.start,
        freq_end_mhz=args.end,
        freq_step_mhz=args.step,
        rtlsdr_samples_per_step=args.rtl_samples,
        hackrf_samples_per_step=args.hackrf_samples,
        settling_time_ms=args.settling
    )
    
    # Create output directory
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = Path(__file__).parent / "data" / f"sweep_{session_id}"
        
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"=== Spectrum Sweep Capture ===")
    print(f"Session: {session_id}")
    print(f"Output: {output_dir}")
    print()
    
    # Create sweep capturer
    sweep = SyncedSweepCapture(output_dir, config)
    
    # Detect devices
    print("Detecting devices...")
    if not sweep.detect_devices():
        sys.exit(1)
        
    print()
    print(f"Frequency range: {config.freq_start_mhz} - {config.freq_end_mhz} MHz")
    print(f"Step size: {config.freq_step_mhz} MHz")
    print(f"Total steps: {config.num_steps}")
    print(f"RTL-SDR active: ≤{config.rtlsdr_max_freq_mhz} MHz")
    print(f"HackRF active: full range")
    print(f"Est. duration: {config.estimated_duration_s:.1f}s ({config.estimated_duration_s/60:.1f} min)")
    print()
    
    # Auto-start (use --confirm flag for interactive prompt)
    if hasattr(args, 'confirm') and args.confirm:
        input("Press Enter to start sweep (Ctrl+C to cancel)...")
    
    # Setup interrupt handler
    def signal_handler(sig, frame):
        print("\n\nInterrupt received, stopping...")
        sweep.running = False
        
    signal.signal(signal.SIGINT, signal_handler)
    
    # Run sweep
    sweep.run_sweep()
    
    # Save metadata
    sweep.save_metadata()
    
    # Summary
    print(f"\n=== Sweep Summary ===")
    print(f"Frequencies captured: {len(sweep.captures)}")
    print(f"Frames captured: {sweep.frame_count}")
    
    # Data stats
    total_left = sum(c.samples_left for c in sweep.captures)
    total_right = sum(c.samples_right for c in sweep.captures)
    total_hackrf = sum(c.samples_hackrf for c in sweep.captures)
    
    print(f"\nTotal samples:")
    print(f"  RTL-SDR Left:  {total_left:,}")
    print(f"  RTL-SDR Right: {total_right:,}")
    print(f"  HackRF:        {total_hackrf:,}")
    
    # Calculate total data
    iq_dir = output_dir / "iq_data"
    if iq_dir.exists():
        total_bytes = sum(f.stat().st_size for f in iq_dir.glob("*.bin"))
        print(f"\nTotal IQ data: {total_bytes / 1024 / 1024:.1f} MB")
        
    print(f"\nOutput directory: {output_dir}")


if __name__ == "__main__":
    main()
