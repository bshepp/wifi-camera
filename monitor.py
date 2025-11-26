#!/usr/bin/env python3
"""
WiFi Camera - Stream Monitor

Standalone tool to monitor active captures and SDR device status.
Run in a separate terminal while capture.py is running.

Usage:
    python monitor.py              # Monitor active capture
    python monitor.py --devices    # Check device status only
    python monitor.py --live       # Continuous monitoring (Ctrl+C to stop)
"""

import os
import sys
import time
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# ANSI color codes
class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    DIM = "\033[2m"


def get_terminal_width() -> int:
    """Get terminal width for formatting"""
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def format_bytes(size: int) -> str:
    """Format bytes to human readable"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_rate(rate: float) -> str:
    """Format data rate to human readable"""
    if rate < 1024:
        return f"{rate:.1f} B/s"
    elif rate < 1024 * 1024:
        return f"{rate/1024:.1f} KB/s"
    else:
        return f"{rate/1024/1024:.1f} MB/s"


def check_process_running(name: str) -> List[Dict]:
    """Check if a process is running and get its info"""
    processes = []
    try:
        result = subprocess.run(
            ["pgrep", "-af", name],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split('\n'):
            if line and name in line:
                parts = line.split(None, 1)
                if len(parts) >= 2:
                    processes.append({
                        "pid": int(parts[0]),
                        "command": parts[1][:60]
                    })
    except Exception:
        pass
    return processes


def get_active_capture_dir() -> Optional[Path]:
    """Find the most recent/active capture directory"""
    data_dir = Path.home() / "Projects/wifi-camera/data"
    if not data_dir.exists():
        return None

    # Get all session directories sorted by modification time
    sessions = sorted(
        [d for d in data_dir.iterdir() if d.is_dir()],
        key=lambda x: x.stat().st_mtime,
        reverse=True
    )

    if sessions:
        return sessions[0]
    return None


def monitor_files(directory: Path) -> Dict[str, Dict]:
    """Monitor file sizes and calculate rates"""
    files = {}

    for filepath in directory.glob("*.bin"):
        try:
            stat = filepath.stat()
            files[filepath.name] = {
                "path": filepath,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            }
        except OSError:
            pass

    return files


def calculate_rates(prev_files: Dict, curr_files: Dict,
                    elapsed: float) -> Dict[str, float]:
    """Calculate data rates from file size changes"""
    rates = {}

    for name, curr in curr_files.items():
        if name in prev_files and elapsed > 0:
            size_diff = curr["size"] - prev_files[name]["size"]
            rates[name] = size_diff / elapsed
        else:
            rates[name] = 0.0

    return rates


def check_sdr_devices() -> Dict[str, Dict]:
    """Check SDR device status"""
    devices = {
        "rtlsdr": [],
        "hackrf": None,
    }

    # Check RTL-SDR
    try:
        result = subprocess.run(
            ["rtl_test", "-t"],
            capture_output=True, text=True, timeout=10
        )
        output = result.stdout + result.stderr

        import re
        match = re.search(r'Found (\d+) device', output)
        if match:
            count = int(match.group(1))
            # Parse device info
            device_pattern = re.compile(r'(\d+):\s+(\w+),\s+(\w+),\s+SN:\s+(\w+)')
            for m in device_pattern.finditer(output):
                devices["rtlsdr"].append({
                    "index": int(m.group(1)),
                    "vendor": m.group(2),
                    "product": m.group(3),
                    "serial": m.group(4),
                })
    except Exception as e:
        pass

    # Check HackRF
    try:
        result = subprocess.run(
            ["hackrf_info"],
            capture_output=True, text=True, timeout=10
        )
        if "Found HackRF" in result.stdout:
            import re
            serial_match = re.search(r'Serial number:\s+(\w+)', result.stdout)
            devices["hackrf"] = {
                "serial": serial_match.group(1) if serial_match else "unknown",
                "available": True,
            }
    except Exception:
        pass

    return devices


def check_webcam() -> Optional[Dict]:
    """Check webcam status"""
    video_path = Path("/dev/video0")
    if video_path.exists():
        # Get name from sysfs
        name_path = Path("/sys/class/video4linux/video0/name")
        name = "Unknown"
        if name_path.exists():
            try:
                name = name_path.read_text().strip()
            except:
                pass
        return {"device": "/dev/video0", "name": name}
    return None


def print_header(title: str):
    """Print a formatted header"""
    width = get_terminal_width()
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'═' * width}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{title.center(width)}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'═' * width}{Colors.RESET}")


def print_device_status():
    """Print device status"""
    print_header("WiFi Camera - Device Status")

    devices = check_sdr_devices()
    webcam = check_webcam()

    # RTL-SDR
    print(f"\n{Colors.BOLD}RTL-SDR Devices:{Colors.RESET}")
    if devices["rtlsdr"]:
        for dev in devices["rtlsdr"]:
            print(f"  {Colors.GREEN}●{Colors.RESET} [{dev['index']}] {dev['vendor']} {dev['product']} (SN: {dev['serial']})")
    else:
        print(f"  {Colors.RED}●{Colors.RESET} None detected")

    # HackRF
    print(f"\n{Colors.BOLD}HackRF:{Colors.RESET}")
    if devices["hackrf"]:
        print(f"  {Colors.GREEN}●{Colors.RESET} Serial: {devices['hackrf']['serial']}")
    else:
        print(f"  {Colors.RED}●{Colors.RESET} Not detected")

    # Webcam
    print(f"\n{Colors.BOLD}Webcam:{Colors.RESET}")
    if webcam:
        print(f"  {Colors.GREEN}●{Colors.RESET} {webcam['device']} - {webcam['name']}")
    else:
        print(f"  {Colors.RED}●{Colors.RESET} Not detected")

    # Running processes
    print(f"\n{Colors.BOLD}SDR Processes:{Colors.RESET}")

    rtl_procs = check_process_running("rtl_sdr")
    hackrf_procs = check_process_running("hackrf_transfer")
    ffmpeg_procs = check_process_running("ffmpeg")

    if rtl_procs:
        for proc in rtl_procs:
            print(f"  {Colors.GREEN}●{Colors.RESET} rtl_sdr (PID {proc['pid']})")
    else:
        print(f"  {Colors.DIM}  rtl_sdr: not running{Colors.RESET}")

    if hackrf_procs:
        for proc in hackrf_procs:
            print(f"  {Colors.GREEN}●{Colors.RESET} hackrf_transfer (PID {proc['pid']})")
    else:
        print(f"  {Colors.DIM}  hackrf_transfer: not running{Colors.RESET}")

    if ffmpeg_procs:
        for proc in ffmpeg_procs:
            print(f"  {Colors.GREEN}●{Colors.RESET} ffmpeg (PID {proc['pid']})")
    else:
        print(f"  {Colors.DIM}  ffmpeg: not running{Colors.RESET}")

    print()


def print_capture_status(capture_dir: Path, rates: Dict[str, float]):
    """Print capture status for a directory"""
    files = monitor_files(capture_dir)

    # Session info
    session_id = capture_dir.name

    # Check for metadata
    metadata_file = capture_dir / "metadata.json"
    metadata = {}
    if metadata_file.exists():
        import json
        try:
            with open(metadata_file) as f:
                metadata = json.load(f)
        except:
            pass

    print(f"\n{Colors.BOLD}Session:{Colors.RESET} {session_id}")
    if metadata:
        freq_mhz = metadata.get("frequency_hz", 0) / 1e6
        channel = metadata.get("wifi_channel", "?")
        print(f"{Colors.DIM}Channel {channel} ({freq_mhz:.1f} MHz){Colors.RESET}")

    print(f"\n{Colors.BOLD}{'File':<20} {'Size':>12} {'Rate':>12} {'Status':<10}{Colors.RESET}")
    print(f"{'-'*56}")

    # Expected rates for "active" detection
    expected_rates = {
        "rtlsdr_left.bin": 3_000_000,   # ~3 MB/s
        "rtlsdr_right.bin": 3_000_000,
        "hackrf.bin": 30_000_000,        # ~30 MB/s
        "webcam.mkv": 500_000,           # ~0.5 MB/s
    }

    total_size = 0
    total_rate = 0

    for name, info in sorted(files.items()):
        size = info["size"]
        rate = rates.get(name, 0)
        total_size += size
        total_rate += rate

        # Determine status
        expected = expected_rates.get(name, 1_000_000)
        if rate > expected * 0.5:
            status = f"{Colors.GREEN}ACTIVE{Colors.RESET}"
        elif rate > 0:
            status = f"{Colors.YELLOW}SLOW{Colors.RESET}"
        else:
            status = f"{Colors.DIM}idle{Colors.RESET}"

        # Color the rate based on activity
        if rate > expected * 0.5:
            rate_str = f"{Colors.GREEN}{format_rate(rate)}{Colors.RESET}"
        elif rate > 0:
            rate_str = f"{Colors.YELLOW}{format_rate(rate)}{Colors.RESET}"
        else:
            rate_str = f"{Colors.DIM}{format_rate(rate)}{Colors.RESET}"

        print(f"{name:<20} {format_bytes(size):>12} {rate_str:>22} {status}")

    print(f"{'-'*56}")
    print(f"{'TOTAL':<20} {format_bytes(total_size):>12} {format_rate(total_rate):>12}")

    # Webcam status
    webcam_file = capture_dir / "webcam.mkv"
    if webcam_file.exists():
        print(f"\n{Colors.BOLD}Webcam:{Colors.RESET} {format_bytes(webcam_file.stat().st_size)}")


def monitor_once():
    """Single monitoring snapshot"""
    print_device_status()

    capture_dir = get_active_capture_dir()
    if capture_dir:
        # Get initial file sizes
        prev_files = monitor_files(capture_dir)
        time.sleep(1)
        curr_files = monitor_files(capture_dir)
        rates = calculate_rates(prev_files, curr_files, 1.0)

        print_header("Active Capture")
        print_capture_status(capture_dir, rates)
    else:
        print(f"\n{Colors.YELLOW}No capture sessions found.{Colors.RESET}")
        print(f"Start a capture with: {Colors.CYAN}python capture.py{Colors.RESET}")

    print()


def monitor_live(interval: float = 1.0):
    """Continuous monitoring"""
    print(f"{Colors.BOLD}Live monitoring (Ctrl+C to stop){Colors.RESET}")
    print(f"Refresh interval: {interval}s\n")

    prev_files = {}
    prev_time = time.time()

    try:
        while True:
            # Clear screen
            print("\033[2J\033[H", end="")

            print_header(f"WiFi Camera Monitor - {datetime.now().strftime('%H:%M:%S')}")

            # Check processes
            rtl_procs = check_process_running("rtl_sdr")
            hackrf_procs = check_process_running("hackrf_transfer")
            ffmpeg_procs = check_process_running("ffmpeg")

            print(f"\n{Colors.BOLD}Active Processes:{Colors.RESET}", end="")
            if rtl_procs:
                print(f" {Colors.GREEN}RTL-SDR({len(rtl_procs)}){Colors.RESET}", end="")
            if hackrf_procs:
                print(f" {Colors.GREEN}HackRF{Colors.RESET}", end="")
            if ffmpeg_procs:
                print(f" {Colors.GREEN}Webcam{Colors.RESET}", end="")
            if not (rtl_procs or hackrf_procs or ffmpeg_procs):
                print(f" {Colors.DIM}none{Colors.RESET}", end="")
            print()

            # Find active capture
            capture_dir = get_active_capture_dir()
            if capture_dir:
                curr_time = time.time()
                curr_files = monitor_files(capture_dir)

                elapsed = curr_time - prev_time
                if elapsed > 0:
                    rates = calculate_rates(prev_files, curr_files, elapsed)
                else:
                    rates = {}

                print_capture_status(capture_dir, rates)

                prev_files = curr_files
                prev_time = curr_time
            else:
                print(f"\n{Colors.YELLOW}No active capture{Colors.RESET}")

            print(f"\n{Colors.DIM}Press Ctrl+C to stop{Colors.RESET}")
            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n{Colors.BOLD}Monitoring stopped.{Colors.RESET}")


def main():
    parser = argparse.ArgumentParser(
        description="WiFi Camera - Stream Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python monitor.py              # Single status snapshot
  python monitor.py --devices    # Check device availability
  python monitor.py --live       # Continuous monitoring
  python monitor.py --live -i 2  # Update every 2 seconds
        """
    )
    parser.add_argument(
        "--devices", "-d", action="store_true",
        help="Only show device status"
    )
    parser.add_argument(
        "--live", "-l", action="store_true",
        help="Continuous live monitoring"
    )
    parser.add_argument(
        "--interval", "-i", type=float, default=1.0,
        help="Update interval for live mode (default: 1.0s)"
    )

    args = parser.parse_args()

    if args.devices:
        print_device_status()
    elif args.live:
        monitor_live(args.interval)
    else:
        monitor_once()


if __name__ == "__main__":
    main()
