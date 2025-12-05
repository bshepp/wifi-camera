"""
Device Detection and Identification

Handles detection and consistent identification of:
- RTL-SDR dongles (by USB bus/port path)
- HackRF One
- Webcam
- GPS receiver
"""

import subprocess
import re
import os
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
from pathlib import Path


@dataclass
class USBDevice:
    """Represents a USB device"""
    bus: int
    device: int
    vendor_id: str
    product_id: str
    name: str
    port_path: str  # e.g., "1-4.2" for bus 1, port 4, subport 2


@dataclass
class RTLSDRDevice:
    """RTL-SDR device info"""
    index: int
    name: str
    serial: str
    usb_path: str
    position: str  # "left", "right", or "unknown"


@dataclass
class HackRFDevice:
    """HackRF device info"""
    index: int
    serial: str
    firmware_version: str
    usb_path: str


@dataclass
class WebcamDevice:
    """Webcam device info"""
    device_path: str  # e.g., /dev/video0
    name: str
    usb_path: str


@dataclass
class GPSDevice:
    """GPS device info"""
    device_path: str  # e.g., /dev/ttyUSB0
    name: str
    usb_path: str


class DeviceManager:
    """Manages detection and identification of all capture devices"""

    # Known USB paths for device positions (update based on your setup)
    # These are determined by physical USB port connections
    RTLSDR_POSITIONS = {
        "1-4.2": "left",   # Bus 1, hub port 4, subport 2
        "1-4.3": "right",  # Bus 1, hub port 4, subport 3
    }

    def __init__(self):
        self.rtlsdr_devices: List[RTLSDRDevice] = []
        self.hackrf_device: Optional[HackRFDevice] = None
        self.webcam_device: Optional[WebcamDevice] = None
        self.gps_device: Optional[GPSDevice] = None

    def detect_all(self) -> Dict:
        """Detect all devices and return summary"""
        self.detect_rtlsdr()
        self.detect_hackrf()
        self.detect_webcam()
        self.detect_gps()

        return {
            "rtlsdr": self.rtlsdr_devices,
            "hackrf": self.hackrf_device,
            "webcam": self.webcam_device,
            "gps": self.gps_device,
        }

    def _get_usb_path_for_device(self, bus: int, device: int) -> str:
        """Get USB port path for a device using sysfs"""
        try:
            # Find the device in sysfs
            usb_devices = Path("/sys/bus/usb/devices")
            for dev_path in usb_devices.iterdir():
                if dev_path.name.startswith("usb"):
                    continue
                try:
                    busnum_path = dev_path / "busnum"
                    devnum_path = dev_path / "devnum"
                    if busnum_path.exists() and devnum_path.exists():
                        if (int(busnum_path.read_text().strip()) == bus and
                                int(devnum_path.read_text().strip()) == device):
                            return dev_path.name
                except (ValueError, IOError):
                    continue
        except Exception:
            pass
        return f"{bus}-unknown"

    def _get_usb_path_by_vid_pid(self, vendor_id: str, product_id: str,
                                  instance: int = 0) -> str:
        """Get USB path for device by vendor/product ID"""
        try:
            result = subprocess.run(
                ["lsusb", "-d", f"{vendor_id}:{product_id}"],
                capture_output=True, text=True, timeout=5
            )
            matches = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    # Parse: Bus 001 Device 007: ID 0bda:2838 ...
                    match = re.match(r'Bus (\d+) Device (\d+):', line)
                    if match:
                        bus = int(match.group(1))
                        dev = int(match.group(2))
                        usb_path = self._get_usb_path_for_device(bus, dev)
                        matches.append((bus, dev, usb_path))

            if instance < len(matches):
                return matches[instance][2]
        except Exception:
            pass
        return "unknown"

    def detect_rtlsdr(self) -> List[RTLSDRDevice]:
        """Detect RTL-SDR devices and identify by USB path"""
        self.rtlsdr_devices = []

        try:
            # Use rtl_test to enumerate devices
            result = subprocess.run(
                ["rtl_test", "-t"],
                capture_output=True, text=True, timeout=10
            )
            output = result.stdout + result.stderr

            # Parse device count
            match = re.search(r'Found (\d+) device', output)
            if not match:
                return self.rtlsdr_devices

            num_devices = int(match.group(1))

            # Get USB paths for each RTL-SDR (VID:PID = 0bda:2838)
            usb_paths = []
            try:
                lsusb_result = subprocess.run(
                    ["lsusb", "-d", "0bda:2838"],
                    capture_output=True, text=True, timeout=5
                )
                for line in lsusb_result.stdout.strip().split('\n'):
                    if line:
                        match = re.match(r'Bus (\d+) Device (\d+):', line)
                        if match:
                            bus = int(match.group(1))
                            dev = int(match.group(2))
                            path = self._get_usb_path_for_device(bus, dev)
                            usb_paths.append(path)
            except Exception:
                usb_paths = ["unknown"] * num_devices

            # Parse device info
            device_pattern = re.compile(
                r'(\d+):\s+(\w+),\s+(.+?),\s+SN:\s+(\w+)'
            )
            for match in device_pattern.finditer(output):
                idx = int(match.group(1))
                vendor = match.group(2)
                product = match.group(3)
                serial = match.group(4)

                usb_path = usb_paths[idx] if idx < len(usb_paths) else "unknown"
                position = self.RTLSDR_POSITIONS.get(usb_path, "unknown")

                self.rtlsdr_devices.append(RTLSDRDevice(
                    index=idx,
                    name=f"{vendor} {product}",
                    serial=serial,
                    usb_path=usb_path,
                    position=position,
                ))

        except subprocess.TimeoutExpired:
            pass
        except FileNotFoundError:
            pass

        return self.rtlsdr_devices

    def detect_hackrf(self) -> Optional[HackRFDevice]:
        """Detect HackRF device"""
        self.hackrf_device = None

        try:
            result = subprocess.run(
                ["hackrf_info"],
                capture_output=True, text=True, timeout=10
            )

            if "Found HackRF" in result.stdout:
                serial_match = re.search(
                    r'Serial number:\s+(\w+)', result.stdout
                )
                fw_match = re.search(
                    r'Firmware Version:\s+([^\n]+)', result.stdout
                )

                serial = serial_match.group(1) if serial_match else "unknown"
                firmware = fw_match.group(1) if fw_match else "unknown"

                # Get USB path (VID:PID = 1d50:6089)
                usb_path = self._get_usb_path_by_vid_pid("1d50", "6089")

                self.hackrf_device = HackRFDevice(
                    index=0,
                    serial=serial,
                    firmware_version=firmware,
                    usb_path=usb_path,
                )

        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        return self.hackrf_device

    def detect_webcam(self) -> Optional[WebcamDevice]:
        """Detect webcam device"""
        self.webcam_device = None

        video_devices = sorted(Path("/dev").glob("video*"))
        for dev_path in video_devices:
            try:
                # Get device name from sysfs
                video_name = dev_path.name
                sysfs_path = Path(f"/sys/class/video4linux/{video_name}")
                if sysfs_path.exists():
                    name_file = sysfs_path / "name"
                    if name_file.exists():
                        name = name_file.read_text().strip()
                        # Skip metadata devices (usually odd-numbered)
                        if "Webcam" in name or "Camera" in name:
                            # Get USB path
                            device_link = sysfs_path / "device"
                            usb_path = "unknown"
                            if device_link.exists():
                                real_path = device_link.resolve()
                                # Extract USB path from sysfs path
                                parts = str(real_path).split('/')
                                for part in parts:
                                    if re.match(r'\d+-[\d.]+', part):
                                        usb_path = part
                                        break

                            self.webcam_device = WebcamDevice(
                                device_path=str(dev_path),
                                name=name,
                                usb_path=usb_path,
                            )
                            return self.webcam_device
            except Exception:
                continue

        return self.webcam_device

    def detect_gps(self) -> Optional[GPSDevice]:
        """Detect GPS device (u-blox USB or FTDI interface)"""
        self.gps_device = None

        # First check for u-blox GPS on ttyACM* (USB CDC/ACM devices)
        acm_devices = sorted(Path("/dev").glob("ttyACM*"))
        for dev_path in acm_devices:
            try:
                sysfs_path = Path(f"/sys/class/tty/{dev_path.name}")
                if sysfs_path.exists():
                    device_link = sysfs_path / "device"
                    if device_link.exists():
                        # Get product name
                        product_path = device_link / "../product"
                        if product_path.exists():
                            product = product_path.read_text().strip()
                            if "u-blox" in product.lower() or "gps" in product.lower():
                                # Get USB path
                                usb_path = "unknown"
                                real_path = str(device_link.resolve())
                                parts = real_path.split('/')
                                for part in parts:
                                    if re.match(r'\d+-[\d.]+', part):
                                        usb_path = part
                                        break

                                self.gps_device = GPSDevice(
                                    device_path=str(dev_path),
                                    name=product,
                                    usb_path=usb_path,
                                )
                                return self.gps_device
            except Exception:
                continue

        # Fall back to FTDI device (internal SiRF GPS on some laptops)
        tty_devices = sorted(Path("/dev").glob("ttyUSB*"))
        for dev_path in tty_devices:
            try:
                sysfs_path = Path(f"/sys/class/tty/{dev_path.name}")
                if sysfs_path.exists():
                    device_link = sysfs_path / "device"
                    if device_link.exists():
                        real_path = str(device_link.resolve())
                        if "ftdi" in real_path.lower():
                            usb_path = "unknown"
                            parts = real_path.split('/')
                            for part in parts:
                                if re.match(r'\d+-[\d.]+', part):
                                    usb_path = part

                            self.gps_device = GPSDevice(
                                device_path=str(dev_path),
                                name="SiRF GPS (FTDI)",
                                usb_path=usb_path,
                            )
                            return self.gps_device
            except Exception:
                continue

        return self.gps_device

    def get_rtlsdr_by_position(self, position: str) -> Optional[RTLSDRDevice]:
        """Get RTL-SDR device by position (left/right)"""
        for dev in self.rtlsdr_devices:
            if dev.position == position:
                return dev
        return None

    def print_summary(self):
        """Print detected devices summary"""
        print("=" * 60)
        print("WiFi Camera - Device Detection Summary")
        print("=" * 60)

        print("\nRTL-SDR Devices:")
        if self.rtlsdr_devices:
            for dev in self.rtlsdr_devices:
                print(f"  [{dev.index}] {dev.name}")
                print(f"      Serial: {dev.serial}")
                print(f"      USB Path: {dev.usb_path}")
                print(f"      Position: {dev.position.upper()}")
        else:
            print("  None detected")

        print("\nHackRF:")
        if self.hackrf_device:
            print(f"  Serial: {self.hackrf_device.serial}")
            print(f"  Firmware: {self.hackrf_device.firmware_version}")
            print(f"  USB Path: {self.hackrf_device.usb_path}")
        else:
            print("  Not detected")

        print("\nWebcam:")
        if self.webcam_device:
            print(f"  Device: {self.webcam_device.device_path}")
            print(f"  Name: {self.webcam_device.name}")
            print(f"  USB Path: {self.webcam_device.usb_path}")
        else:
            print("  Not detected")

        print("\nGPS:")
        if self.gps_device:
            print(f"  Device: {self.gps_device.device_path}")
            print(f"  Name: {self.gps_device.name}")
            print(f"  USB Path: {self.gps_device.usb_path}")
        else:
            print("  Not detected")

        print("=" * 60)


def main():
    """Test device detection"""
    manager = DeviceManager()
    manager.detect_all()
    manager.print_summary()


if __name__ == "__main__":
    main()
