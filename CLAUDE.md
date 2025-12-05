# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

WiFi Camera is an experimental passive WiFi radar/imaging system that captures synchronized RF signals from multiple software-defined radios (SDRs) and correlates them with webcam video. The goal is to explore passive radar techniques using ambient 2.4 GHz WiFi signals.

## Hardware Architecture

The system uses 4 synchronized devices:
- **2x RTL-SDR dongles** (surveillance channels): Left and right positions with omnidirectional antennas, 38cm baseline
- **1x HackRF One** (reference channel): Directional log-periodic antenna, 8 MSPS
- **1x Webcam**: Visual ground truth at 1280x720, 30fps

Physical layout:
```
[RTL-SDR LEFT] <--38cm--> [HackRF+WEBCAM] <--38cm--> [RTL-SDR RIGHT]
```

## Key Concepts

**Passive Radar Theory:**
1. Reference signal captures direct WiFi transmissions (HackRF with directional antenna)
2. Surveillance signals capture reflections from objects (RTL-SDRs with omni antennas)
3. Cross-correlation reveals range and Doppler shifts of reflecting targets
4. Phase difference between RTL-SDR pair estimates angle of arrival

**Data Formats:**
- RTL-SDR: **unsigned** 8-bit IQ (center: 127.5), 2.56 MSPS
- HackRF: **signed** 8-bit IQ (center: 0), 8 MSPS
- This format difference is critical and handled in `load_iq_data()` in process.py

## Code Architecture

### Core Modules

**devices.py** - Hardware detection and identification
- `DeviceManager` class handles all device enumeration
- RTL-SDRs identified by USB path (stored in `RTLSDR_POSITIONS` dict)
- Important: RTL-SDRs have identical serial numbers, differentiated only by USB path
- Detects HackRF, webcam, and GPS devices

**config.py** - System configuration
- Dataclasses for device-specific configs: `RTLSDRConfig`, `HackRFConfig`, `WebcamConfig`, `GPSConfig`
- `CaptureConfig` ties everything together
- WiFi channel frequencies defined in `WIFI_CHANNELS_24GHZ` dict
- `__post_init__` automatically converts channel to frequency in Hz

**capture.py** - Main synchronized capture script
- `StreamCapture` class manages individual device capture threads
- `CaptureMetadata` stores complete session information
- Uses subprocess for rtl_sdr, hackrf_transfer, and ffmpeg commands
- Critical: Each device records `first_data_time` when first bytes arrive for time alignment
- Outputs to `data/<session_id>/` with metadata, timing, IQ files, and frames

**process.py** - Signal processing algorithms
- `load_iq_data()` handles both signed (HackRF) and unsigned (RTL-SDR) formats
- Cross-correlation, FFT, range-Doppler processing functions
- `ProcessingParams` dataclass with physical constants (wavelength, range resolution)
- CFAR (Constant False Alarm Rate) detection parameters

**monitor.py** - Real-time capture monitoring
- Standalone tool to watch active captures
- Monitors file sizes, data rates, device processes
- Can run in live mode (`--live`) for continuous monitoring

**sanity_check.py** - Data validation and quality analysis
- Analyzes IQ data: DC offset, power levels, SNR, spectrum
- Validates frame timestamps and timing synchronization
- Identifies capture issues (clipping, weak signals, timing gaps)

**sync.py** - Synchronization analysis module
- Cross-correlation based stream alignment (more accurate than timestamps)
- Clock drift measurement between RTL-SDR devices
- Sample loss detection and validation
- Generates comprehensive sync reports with warnings/recommendations

**gps.py** - GPS time source
- NMEA parsing for u-blox and compatible GPS receivers
- System clock offset measurement vs GPS UTC
- Position metadata for captures
- Background threaded reading

**spectrum_sweep.py** - Full-band synchronized spectrum capture
- Sweeps 25 MHz - 6 GHz with all SDRs + webcam
- Barrier synchronization at each frequency step
- RTL-SDRs active ≤1750 MHz, HackRF covers full range
- Outputs per-frequency IQ files with timestamp correlation
- Modes: `--quick` (test), `--full` (2 hours), `--start/--end` (custom)

## Common Development Tasks

### Environment Setup
```bash
cd ~/Projects/wifi-camera
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Running a Basic Capture
```bash
source venv/bin/activate
python capture.py                    # 10 second default
python capture.py --duration 300     # 5 minutes
python capture.py --channel 1        # WiFi channel 1, 6, or 11
python capture.py --no-hackrf        # Skip HackRF if unavailable
```

### Monitoring a Running Capture
```bash
# In separate terminal
python monitor.py --live             # Continuous monitoring
python monitor.py --devices          # Check device status only
```

### Validating Captured Data
```bash
python sanity_check.py data/<session_id>
```

### Analyzing Synchronization
```bash
# Generate sync report with alignment, drift, and sample loss analysis
python sync.py data/<session_id>
```

### Loading IQ Data for Processing
```python
from process import load_rtlsdr_iq, load_hackrf_iq
import numpy as np

# RTL-SDR (unsigned 8-bit)
iq_left = load_rtlsdr_iq('data/session/rtlsdr_left.bin', samples=1000000)

# HackRF (signed 8-bit)
iq_ref = load_hackrf_iq('data/session/hackrf.bin', samples=1000000)
```

### Time Alignment Between Devices
```python
import json

with open('data/session/timing.json') as f:
    timing = json.load(f)

with open('data/session/frame_timestamps.json') as f:
    frames = json.load(f)

# Calculate IQ sample index for a video frame
frame_time = frames['frames'][100]['timestamp']
rtl_start = timing['streams']['rtlsdr_right']['first_data_time']
sample_rate = timing['sample_rates']['rtlsdr']
sample_index = int((frame_time - rtl_start) * sample_rate)
```

## Output Structure

Each capture session creates:
```
data/<session_id>/
├── rtlsdr_left.bin          # Left RTL-SDR IQ data (uint8)
├── rtlsdr_right.bin         # Right RTL-SDR IQ data (uint8)
├── hackrf.bin               # HackRF IQ data (int8)
├── frames/                  # JPEG frames at ~30fps
│   └── frame_NNNNNN_TIMESTAMP.jpg
├── metadata.json            # Capture configuration
├── timing.json              # Per-device first_data_time for sync
├── frame_timestamps.json    # Per-frame timestamps
└── DATA_FORMAT.md           # Auto-generated format documentation
```

## Important Implementation Details

### Device Detection Order
RTL-SDR devices must be consistently identified across captures:
1. USB path is queried from sysfs
2. Path is matched against `DeviceManager.RTLSDR_POSITIONS` dict
3. Position (left/right) determines which antenna is which for interferometry
4. Update `RTLSDR_POSITIONS` if USB ports change

### Time Synchronization
- All timestamps use `time.time()` (system clock, not GPS yet)
- Each device records `first_data_time` when first bytes arrive
- Capture uses barrier synchronization for coordinated SDR process starts
- `first_data_time` reflects USB buffer delivery, not ADC start
- **Cross-correlation alignment** (via `sync.py`) is more accurate than timestamps
- Typical alignment: <10ms between RTL-SDRs after correlation
- Clock drift: ~10-50 ppm between RTL-SDRs (measurable over 5+ minutes)
- Frame timestamps embedded in filenames: `frame_000000_1763938203.738430.jpg`

### Data Rate Management
5-minute capture produces ~8 GB:
- RTL-SDR (each): ~4.8 MB/s → 1.4 GB
- HackRF: ~10 MB/s → 3 GB
- Webcam: ~5 MB/s → 2 GB

Total sustained rate: ~25 MB/s

### Signal Processing Workflow
1. Load IQ data with correct signedness (unsigned for RTL-SDR, signed for HackRF)
2. Normalize to [-1, 1] range (formulas differ by device)
3. Cross-correlate reference (HackRF) with surveillance (RTL-SDR)
4. FFT for Doppler processing
5. Phase difference between RTL-SDR pair for angle estimation

### Physical Parameters
- Channel 6: 2437 MHz → wavelength 12.5 cm
- RTL-SDR bandwidth: 2.4 MHz → range resolution 62.5m
- HackRF bandwidth: 10 MHz → range resolution 15m
- Antenna baseline: 38 cm (~3 wavelengths at 2.4 GHz)

## Known Issues & Quirks

1. **RTL-SDR Serial Numbers**: Both devices report "00000001" - must use USB path
2. **Signed vs Unsigned IQ**: HackRF uses int8, RTL-SDR uses uint8 - easy to mix up
3. **Antenna Mismatch**: Current setup uses mismatched antennas → weaker correlation
4. **GPS Time Offset**: GPS provides system clock offset measurement (~285ms typical)
5. **Buffer Overflows**: At high CPU load, SDR processes may drop samples
6. **RTL-SDR Thermal Throttling**: Devices overheat during extended captures (>30 min). Add heatsinks or cooling breaks. Full spectrum sweeps (~2 hours) will likely cause thermal crashes without cooling.

## Testing & Validation

Successful capture indicators (from sanity_check.py):
- SNR: 20-50 dB across all devices
- DC offset: I/Q mean near 0.0 (±0.1)
- Frame rate: 29.5-30.5 fps
- Timing gaps: <100ms between devices
- No clipping: I/Q values not saturated at min/max

## Future Development Areas

1. **GPU Processing**: CuPy/CUDA for real-time correlation (RTX 4090 available)
2. **GPS PPS Sync**: Hardware timing from GPS 1PPS signal (u-blox 7 installed)
3. **Real-time Processing**: Live correlation display during capture
4. **Matched Antennas**: Identical antennas for better phase coherence
5. **Machine Learning**: Pattern recognition correlating RF and visual data

## 915 MHz Bistatic Radar Subsystem

**Location:** `radar_test_915mhz/`

**Rationale:** RTL-SDR dongles (NESDR SMArt v5) have a maximum frequency of 1.75 GHz and cannot receive 2.4 GHz WiFi signals. A separate 915 MHz ISM band passive radar system was created for testing and development.

**Hardware Configuration:**
```
[RTL-SDR LEFT] <--38cm--> [HackRF TX] <--38cm--> [RTL-SDR RIGHT]
                            [WEBCAM]
```

**Key Differences from Main System:**
- **Frequency:** 915 MHz (902-928 MHz ISM band)
- **HackRF:** Transmits CW beacon (`beacon_900mhz_cw.iq`) - not receiving
- **RTL-SDRs:** Both act as surveillance channels (bistatic configuration)
- **Sample Rate:** 2.56 MSPS (same as main system)
- **Synchronization:** Uses same barrier sync and timing infrastructure

**Main Script:** `capture_bistatic_915.py`
- Simplified version of `capture.py` adapted for bistatic operation
- Records `first_data_time` for microsecond-precision synchronization
- Outputs same structure: timing.json, metadata.json, IQ bins, frames/

**Validation Results (5-minute test):**
```
Capture duration: 302.1s
Streams:
  ✓ rtlsdr_left:  722,993,152 samples - loss: -0.02%
  ✓ rtlsdr_right: 723,255,296 samples - loss: -0.06%
  ✓ webcam: 9,000 frames @ 29.8 fps

Cross-correlation:
  Offset: +40,120 samples (+16.72 ms)
  Confidence: 1.00 (perfect!)
  Clock drift: 0.76 ppm
```

**Status:** Successfully validated. Ready for passive radar processing development before implementing 2.4 GHz downconverters for WiFi operation.

**Documentation:** See `radar_test_915mhz/README.md` for complete usage guide.

## System Context

This system runs on Dell Latitude 7404/7414 (Ubuntu 24.04 LTS):
- Hostname: panthro
- User: tempest
- Related projects: data-sponge (mobile sensors), wt6000-remote-access (dashboard)
- See ~/Projects/CLAUDE.md for full system documentation
