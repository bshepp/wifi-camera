#!/usr/bin/env python3
"""
Active Bands Capture - Focused capture on identified active frequency bands

Based on spectrum survey results, captures only the most active bands:
- RTL-SDRs: Sub-1.75 GHz active bands (FM, Air, Marine, Ham, Cellular, ISM)
- HackRF: WiFi bands (2.4 GHz and 5 GHz - strongest signals!)

Each device cycles through its assigned bands, capturing IQ data at each.
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
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
import argparse

sys.path.insert(0, str(Path(__file__).parent))
from devices import DeviceManager


# =============================================================================
# ACTIVE BAND DEFINITIONS (from spectrum survey)
# =============================================================================

# RTL-SDR bands (must be <= 1750 MHz)
RTLSDR_BANDS = [
    {"name": "fm_radio",     "center": 98_000_000,   "description": "FM Radio 88-108 MHz"},
    {"name": "air_band",     "center": 127_000_000,  "description": "Air Band 118-137 MHz"},
    {"name": "2m_ham",       "center": 146_000_000,  "description": "2m Ham 144-148 MHz"},
    {"name": "marine_vhf",   "center": 159_000_000,  "description": "Marine VHF 156-163 MHz"},
    {"name": "70cm_ham",     "center": 435_000_000,  "description": "70cm Ham 420-450 MHz"},
    {"name": "uhf_tv",       "center": 580_000_000,  "description": "UHF TV 470-698 MHz"},
    {"name": "cell_700",     "center": 750_000_000,  "description": "Cellular 700 MHz"},
    {"name": "cell_850",     "center": 859_000_000,  "description": "Cellular 850 MHz"},
    {"name": "ism_900",      "center": 915_000_000,  "description": "ISM 900 MHz"},
    {"name": "gps_l2",       "center": 1227_600_000, "description": "GPS L2 1227.6 MHz"},
]

# HackRF bands (focus on WiFi - strongest signals)
HACKRF_BANDS = [
    {"name": "wifi_2g_ch1",  "center": 2412_000_000, "description": "WiFi 2.4GHz Ch1"},
    {"name": "wifi_2g_ch6",  "center": 2437_000_000, "description": "WiFi 2.4GHz Ch6"},
    {"name": "wifi_2g_ch11", "center": 2462_000_000, "description": "WiFi 2.4GHz Ch11"},
    {"name": "wifi_5g_low",  "center": 5180_000_000, "description": "WiFi 5GHz Low (Ch36)"},
    {"name": "wifi_5g_mid",  "center": 5500_000_000, "description": "WiFi 5GHz Mid (Ch100)"},
    {"name": "wifi_5g_high", "center": 5745_000_000, "description": "WiFi 5GHz High (Ch149)"},
    {"name": "lte_2600",     "center": 2600_000_000, "description": "LTE 2600 MHz"},
]

# Preset band groups
BAND_PRESETS = {
    "wifi": {
        "rtlsdr": [],  # RTL-SDR can't do WiFi frequencies
        "hackrf": ["wifi_2g_ch1", "wifi_2g_ch6", "wifi_2g_ch11", 
                   "wifi_5g_low", "wifi_5g_mid", "wifi_5g_high"]
    },
    "wifi_5g": {
        "rtlsdr": [],
        "hackrf": ["wifi_5g_low", "wifi_5g_mid", "wifi_5g_high"]
    },
    "wifi_2g": {
        "rtlsdr": [],
        "hackrf": ["wifi_2g_ch1", "wifi_2g_ch6", "wifi_2g_ch11"]
    },
    "cellular": {
        "rtlsdr": ["cell_700", "cell_850", "ism_900"],
        "hackrf": ["lte_2600"]
    },
    "all": {
        "rtlsdr": [b["name"] for b in RTLSDR_BANDS],
        "hackrf": [b["name"] for b in HACKRF_BANDS]
    },
    "survey": {
        "rtlsdr": ["fm_radio", "air_band", "marine_vhf", "ism_900", "gps_l2"],
        "hackrf": ["wifi_2g_ch6", "wifi_5g_low", "wifi_5g_high"]
    }
}


@dataclass
class CaptureConfig:
    """Configuration for active bands capture"""
    duration_seconds: int = 300  # 5 minutes default
    
    # RTL-SDR settings
    rtlsdr_sample_rate: int = 2_560_000
    rtlsdr_samples_per_band: int = 2_560_000  # 1 second per band
    rtlsdr_gain: str = "auto"
    rtlsdr_bands: List[str] = field(default_factory=lambda: ["ism_900", "gps_l2"])
    
    # HackRF settings
    hackrf_sample_rate: int = 8_000_000
    hackrf_samples_per_band: int = 8_000_000  # 1 second per band
    hackrf_lna_gain: int = 32
    hackrf_vga_gain: int = 40
    hackrf_bands: List[str] = field(default_factory=lambda: ["wifi_5g_low", "wifi_5g_high"])
    
    dwell_time_ms: int = 50  # Settling time between bands


class BandCapture:
    """Captures IQ data from active bands"""
    
    def __init__(self, output_dir: Path, config: CaptureConfig):
        self.output_dir = output_dir
        self.config = config
        self.dev_mgr = DeviceManager()
        
        # Devices
        self.rtlsdr_left = None
        self.rtlsdr_right = None
        self.hackrf = None
        self.webcam = None
        
        # Control
        self.stop_event = threading.Event()
        self.start_time = 0
        
        # Stats
        self.captures = {"left": [], "right": [], "hackrf": []}
        self.errors = {"left": 0, "right": 0, "hackrf": 0}
        
        # Webcam
        self.webcam_process = None
        self.frame_count = 0
        
    def detect_devices(self) -> bool:
        """Detect available devices"""
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
            
        self.webcam = self.dev_mgr.detect_webcam()
        if self.webcam:
            print(f"✓ Webcam: {self.webcam.device_path}")
            
        return self.hackrf is not None or self.rtlsdr_left is not None
    
    def get_band_info(self, band_name: str, device_type: str) -> Optional[Dict]:
        """Get band info by name"""
        bands = RTLSDR_BANDS if device_type == "rtlsdr" else HACKRF_BANDS
        for band in bands:
            if band["name"] == band_name:
                return band
        return None
    
    def start_webcam(self):
        """Start webcam capture"""
        if not self.webcam:
            return
            
        frames_dir = self.output_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        
        cmd = [
            'ffmpeg', '-y',
            '-f', 'v4l2',
            '-framerate', '30',
            '-video_size', '1280x720',
            '-i', self.webcam.device_path,
            '-t', str(self.config.duration_seconds + 30),
            '-q:v', '2',
            f'{frames_dir}/frame_%06d.jpg'
        ]
        
        self.webcam_process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        print("✓ Webcam started")
        
    def stop_webcam(self):
        """Stop webcam"""
        if self.webcam_process:
            self.webcam_process.terminate()
            try:
                self.webcam_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.webcam_process.kill()
                
        frames_dir = self.output_dir / "frames"
        if frames_dir.exists():
            self.frame_count = len(list(frames_dir.glob("frame_*.jpg")))
    
    def capture_rtlsdr(self, device_index: int, name: str, band: Dict,
                       pass_num: int) -> bool:
        """Capture from RTL-SDR at specified band"""
        freq = band["center"]
        band_name = band["name"]

        filename = f"{name}_{band_name}_p{pass_num:03d}.bin"
        filepath = self.output_dir / "iq_data" / name / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)

        # rtl_sdr's -n is the number of IQ pairs; internally it writes 2 bytes
        # per IQ pair, so the output file ends up at samples_per_band * 2 bytes.
        cmd = [
            'rtl_sdr',
            '-d', str(device_index),
            '-f', str(freq),
            '-s', str(self.config.rtlsdr_sample_rate),
            '-n', str(self.config.rtlsdr_samples_per_band),
            str(filepath)
        ]
        
        if self.config.rtlsdr_gain != "auto":
            cmd.extend(['-g', self.config.rtlsdr_gain])
        
        timestamp = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            
            if filepath.exists() and filepath.stat().st_size > 0:
                self.captures[name].append({
                    "band": band_name,
                    "frequency": freq,
                    "timestamp": timestamp,
                    "pass": pass_num,
                    "filename": filename,
                    "samples": filepath.stat().st_size // 2
                })
                return True
            else:
                self.errors[name] += 1
                return False
                
        except Exception as e:
            self.errors[name] += 1
            return False
    
    def capture_hackrf(self, band: Dict, pass_num: int) -> bool:
        """Capture from HackRF at specified band"""
        freq = band["center"]
        band_name = band["name"]
        
        filename = f"hackrf_{band_name}_p{pass_num:03d}.bin"
        filepath = self.output_dir / "iq_data" / "hackrf" / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        # hackrf_transfer's -n is the number of IQ pairs (it writes 2 bytes per
        # pair). Passing samples_per_band yields exactly that many IQ pairs.
        cmd = [
            'hackrf_transfer',
            '-r', str(filepath),
            '-f', str(freq),
            '-s', str(self.config.hackrf_sample_rate),
            '-l', str(self.config.hackrf_lna_gain),
            '-g', str(self.config.hackrf_vga_gain),
            '-n', str(self.config.hackrf_samples_per_band)
        ]
        
        timestamp = time.time()
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            
            if filepath.exists() and filepath.stat().st_size > 0:
                self.captures["hackrf"].append({
                    "band": band_name,
                    "frequency": freq,
                    "timestamp": timestamp,
                    "pass": pass_num,
                    "filename": filename,
                    "samples": filepath.stat().st_size // 2
                })
                return True
            else:
                self.errors["hackrf"] += 1
                return False
                
        except Exception as e:
            self.errors["hackrf"] += 1
            return False
    
    def rtlsdr_worker(self, device_index: int, name: str):
        """Worker thread for RTL-SDR capture"""
        bands = [self.get_band_info(b, "rtlsdr") for b in self.config.rtlsdr_bands]
        bands = [b for b in bands if b]  # Filter None
        
        if not bands:
            return
            
        pass_num = 0
        while not self.stop_event.is_set():
            pass_num += 1
            
            for band in bands:
                if self.stop_event.is_set():
                    break
                    
                self.capture_rtlsdr(device_index, name, band, pass_num)
                time.sleep(self.config.dwell_time_ms / 1000)
    
    def hackrf_worker(self):
        """Worker thread for HackRF capture"""
        bands = [self.get_band_info(b, "hackrf") for b in self.config.hackrf_bands]
        bands = [b for b in bands if b]
        
        if not bands:
            return
            
        pass_num = 0
        while not self.stop_event.is_set():
            pass_num += 1
            
            for band in bands:
                if self.stop_event.is_set():
                    break
                    
                self.capture_hackrf(band, pass_num)
                time.sleep(self.config.dwell_time_ms / 1000)
    
    def progress_display(self):
        """Show progress"""
        while not self.stop_event.is_set():
            elapsed = time.time() - self.start_time
            remaining = self.config.duration_seconds - elapsed
            
            stats = []
            for name in ["left", "right", "hackrf"]:
                if self.captures[name]:
                    last = self.captures[name][-1]
                    stats.append(f"{name[0].upper()}:{last['band']}")
                    
            line = f"\rT:{int(elapsed)}s/{self.config.duration_seconds}s | " + " | ".join(stats)
            line += f" | Captures: L:{len(self.captures['left'])} R:{len(self.captures['right'])} H:{len(self.captures['hackrf'])}   "
            print(line, end='', flush=True)
            
            time.sleep(1)
    
    def run(self) -> bool:
        """Run the capture"""
        print(f"\n=== Active Bands Capture ===")
        print(f"Duration: {self.config.duration_seconds}s ({self.config.duration_seconds/60:.1f} min)")
        print(f"\nRTL-SDR bands: {', '.join(self.config.rtlsdr_bands) or 'None'}")
        print(f"HackRF bands:  {', '.join(self.config.hackrf_bands) or 'None'}")
        print()
        
        # Create directories
        (self.output_dir / "iq_data").mkdir(parents=True, exist_ok=True)
        
        # Start webcam
        self.start_webcam()
        time.sleep(1)
        
        self.start_time = time.time()
        threads = []
        
        # Start RTL-SDR workers
        if self.config.rtlsdr_bands:
            if self.rtlsdr_left:
                t = threading.Thread(
                    target=self.rtlsdr_worker,
                    args=(self.rtlsdr_left.index, "left"),
                    name="rtlsdr_left"
                )
                threads.append(t)
                
            if self.rtlsdr_right:
                t = threading.Thread(
                    target=self.rtlsdr_worker,
                    args=(self.rtlsdr_right.index, "right"),
                    name="rtlsdr_right"
                )
                threads.append(t)
        
        # Start HackRF worker
        if self.hackrf and self.config.hackrf_bands:
            t = threading.Thread(target=self.hackrf_worker, name="hackrf")
            threads.append(t)
        
        # Start progress display
        progress_t = threading.Thread(target=self.progress_display, daemon=True)
        progress_t.start()
        
        # Launch all workers
        for t in threads:
            t.start()
        
        # Wait for duration
        try:
            end_time = self.start_time + self.config.duration_seconds
            while time.time() < end_time and not self.stop_event.is_set():
                time.sleep(1)
                
            print(f"\n\nDuration complete, stopping...")
            self.stop_event.set()
            
        except KeyboardInterrupt:
            print(f"\n\nInterrupt received, stopping...")
            self.stop_event.set()
        
        # Wait for threads
        for t in threads:
            t.join(timeout=10)
        
        # Stop webcam
        self.stop_webcam()
        
        return True
    
    def save_metadata(self):
        """Save capture metadata"""
        metadata = {
            "session_id": self.output_dir.name,
            "start_time": self.start_time,
            "duration_seconds": self.config.duration_seconds,
            "config": {
                "rtlsdr_sample_rate": self.config.rtlsdr_sample_rate,
                "rtlsdr_samples_per_band": self.config.rtlsdr_samples_per_band,
                "rtlsdr_bands": self.config.rtlsdr_bands,
                "hackrf_sample_rate": self.config.hackrf_sample_rate,
                "hackrf_samples_per_band": self.config.hackrf_samples_per_band,
                "hackrf_bands": self.config.hackrf_bands,
                "hackrf_lna_gain": self.config.hackrf_lna_gain,
                "hackrf_vga_gain": self.config.hackrf_vga_gain,
            },
            "band_definitions": {
                "rtlsdr": {b["name"]: b for b in RTLSDR_BANDS},
                "hackrf": {b["name"]: b for b in HACKRF_BANDS}
            },
            "stats": {
                "captures_left": len(self.captures["left"]),
                "captures_right": len(self.captures["right"]),
                "captures_hackrf": len(self.captures["hackrf"]),
                "errors_left": self.errors["left"],
                "errors_right": self.errors["right"],
                "errors_hackrf": self.errors["hackrf"],
                "frames": self.frame_count
            }
        }
        
        with open(self.output_dir / "metadata.json", 'w') as f:
            json.dump(metadata, f, indent=2)
        
        # Save detailed capture logs
        for name in ["left", "right", "hackrf"]:
            if self.captures[name]:
                with open(self.output_dir / f"captures_{name}.json", 'w') as f:
                    json.dump(self.captures[name], f, indent=2)
        
        print(f"✓ Saved metadata.json")


def main():
    parser = argparse.ArgumentParser(
        description='Active Bands Capture - Focus on identified active frequencies'
    )
    
    parser.add_argument('--duration', '-t', type=int, default=300,
                        help='Capture duration in seconds (default: 300)')
    parser.add_argument('--preset', '-p', type=str, default="wifi_5g",
                        choices=list(BAND_PRESETS.keys()),
                        help='Band preset (default: wifi_5g)')
    parser.add_argument('--rtl-bands', type=str, nargs='+',
                        help='RTL-SDR bands to capture (overrides preset)')
    parser.add_argument('--hackrf-bands', type=str, nargs='+',
                        help='HackRF bands to capture (overrides preset)')
    parser.add_argument('--list-bands', action='store_true',
                        help='List available bands and exit')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Output directory')
    
    args = parser.parse_args()
    
    # List bands and exit
    if args.list_bands:
        print("\n=== RTL-SDR Bands (≤1750 MHz) ===")
        for b in RTLSDR_BANDS:
            print(f"  {b['name']:<15} {b['center']/1e6:>8.1f} MHz  {b['description']}")
        
        print("\n=== HackRF Bands ===")
        for b in HACKRF_BANDS:
            print(f"  {b['name']:<15} {b['center']/1e6:>8.1f} MHz  {b['description']}")
        
        print("\n=== Presets ===")
        for name, bands in BAND_PRESETS.items():
            print(f"  {name}:")
            print(f"    RTL-SDR: {', '.join(bands['rtlsdr']) or 'None'}")
            print(f"    HackRF:  {', '.join(bands['hackrf']) or 'None'}")
        
        return
    
    # Build config
    preset = BAND_PRESETS.get(args.preset, BAND_PRESETS["wifi_5g"])
    
    config = CaptureConfig(
        duration_seconds=args.duration,
        rtlsdr_bands=args.rtl_bands or preset["rtlsdr"],
        hackrf_bands=args.hackrf_bands or preset["hackrf"]
    )
    
    # Create output directory
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = Path(__file__).parent / "data" / f"bands_{session_id}"
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"=== Active Bands Capture ===")
    print(f"Session: {session_id}")
    print(f"Output: {output_dir}")
    print(f"Preset: {args.preset}")
    print()
    
    # Create capturer
    capture = BandCapture(output_dir, config)
    
    # Detect devices
    print("Detecting devices...")
    if not capture.detect_devices():
        print("ERROR: No SDR devices found!")
        sys.exit(1)
    
    # Setup signal handler
    def signal_handler(sig, frame):
        print("\n\nInterrupt received...")
        capture.stop_event.set()
    
    signal.signal(signal.SIGINT, signal_handler)
    
    # Run capture
    capture.run()
    
    # Save metadata
    capture.save_metadata()
    
    # Summary
    print(f"\n=== Capture Summary ===")
    print(f"RTL-SDR Left:  {len(capture.captures['left'])} captures, {capture.errors['left']} errors")
    print(f"RTL-SDR Right: {len(capture.captures['right'])} captures, {capture.errors['right']} errors")
    print(f"HackRF:        {len(capture.captures['hackrf'])} captures, {capture.errors['hackrf']} errors")
    print(f"Webcam:        {capture.frame_count} frames")
    
    # Calculate data size
    iq_dir = output_dir / "iq_data"
    if iq_dir.exists():
        total_bytes = sum(f.stat().st_size for f in iq_dir.rglob("*.bin"))
        print(f"\nTotal IQ data: {total_bytes / 1024 / 1024:.1f} MB")
    
    print(f"\nOutput: {output_dir}")


if __name__ == "__main__":
    main()

