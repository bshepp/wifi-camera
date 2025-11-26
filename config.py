"""
WiFi Camera Configuration
"""

from dataclasses import dataclass, field
from typing import Optional
import os

# WiFi 2.4 GHz Channel Frequencies (MHz)
WIFI_CHANNELS_24GHZ = {
    1: 2412,
    2: 2417,
    3: 2422,
    4: 2427,
    5: 2432,
    6: 2437,
    7: 2442,
    8: 2447,
    9: 2452,
    10: 2457,
    11: 2462,
}

# Most commonly used non-overlapping channels
PRIMARY_CHANNELS = [1, 6, 11]


@dataclass
class RTLSDRConfig:
    """Configuration for RTL-SDR devices"""
    sample_rate: int = 2_400_000  # 2.4 MSPS (max stable)
    frequency: int = 2_437_000_000  # Channel 6 center (Hz)
    gain: float = -1.0  # dB - use -1 for automatic gain (required for faulty LNA device)
    ppm_correction: int = 0
    bias_tee: bool = False

    # Buffer settings
    buffer_size: int = 16 * 16384  # ~262KB per read
    num_buffers: int = 32


@dataclass
class HackRFConfig:
    """Configuration for HackRF One"""
    sample_rate: int = 10_000_000  # 10 MSPS (reduced from 20 to avoid USB saturation)
    frequency: int = 2_437_000_000  # Channel 6 center (Hz)
    lna_gain: int = 32  # 0-40 dB in 8 dB steps
    vga_gain: int = 30  # 0-62 dB in 2 dB steps
    amp_enable: bool = False  # 14 dB amp (usually too much)

    # Bandwidth
    baseband_filter: int = 10_000_000  # 10 MHz (matches sample rate)


@dataclass
class WebcamConfig:
    """Configuration for webcam capture"""
    device: str = "/dev/video0"
    width: int = 1280
    height: int = 720
    fps: int = 30
    format: str = "MJPG"


@dataclass
class GPSConfig:
    """Configuration for GPS timing"""
    device: str = "/dev/ttyACM0"  # u-blox GPS (USB CDC/ACM)
    baudrate: int = 9600  # u-blox default
    use_pps: bool = True
    pps_gpio: Optional[int] = None  # GPIO pin if using hardware PPS
    timeout: float = 1.0  # Serial read timeout


@dataclass
class CaptureConfig:
    """Main capture configuration"""
    # Output
    output_dir: str = field(default_factory=lambda: os.path.expanduser(
        "~/Projects/wifi-camera/data"
    ))

    # Timing
    duration_seconds: float = 10.0
    sync_to_pps: bool = False  # Start capture on PPS edge

    # What to capture
    capture_rtlsdr: bool = True
    capture_hackrf: bool = True
    capture_webcam: bool = True
    capture_gps: bool = True

    # Device configs
    rtlsdr: RTLSDRConfig = field(default_factory=RTLSDRConfig)
    hackrf: HackRFConfig = field(default_factory=HackRFConfig)
    webcam: WebcamConfig = field(default_factory=WebcamConfig)
    gps: GPSConfig = field(default_factory=GPSConfig)

    # Channel selection
    wifi_channel: int = 6

    def __post_init__(self):
        """Update frequencies based on channel selection"""
        if self.wifi_channel in WIFI_CHANNELS_24GHZ:
            freq_mhz = WIFI_CHANNELS_24GHZ[self.wifi_channel]
            self.rtlsdr.frequency = freq_mhz * 1_000_000
            self.hackrf.frequency = freq_mhz * 1_000_000


@dataclass
class ProcessingConfig:
    """Signal processing configuration"""
    # Cross-correlation
    correlation_window_ms: float = 1.0  # Window size for correlation
    correlation_overlap: float = 0.5  # 50% overlap

    # Range processing
    max_range_m: float = 100.0  # Maximum range to process
    range_resolution_m: float = 1.0  # Desired range resolution

    # Doppler processing
    max_doppler_hz: float = 500.0  # Max Doppler shift (walking speed ~3-5 Hz)
    doppler_resolution_hz: float = 1.0

    # Angle processing (interferometry)
    baseline_m: float = 0.38  # 38 cm between RTL-SDRs
    wavelength_m: float = 0.125  # ~12.5 cm at 2.4 GHz


# Default configuration instance
DEFAULT_CONFIG = CaptureConfig()
