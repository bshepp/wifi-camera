# WiFi Camera - Passive WiFi Imaging System

Experimental passive radar/imaging system using ambient 2.4 GHz WiFi signals.

## Overview

This project captures synchronized data from multiple SDRs and a webcam to experiment with passive WiFi radar and potentially WiFi-based imaging. The goal is to correlate ambient WiFi signals with visual reference data to explore what information can be extracted from RF reflections.

## Hardware Configuration

```
    [RTL-SDR LEFT]     [HackRF+LogPeriodic]     [RTL-SDR RIGHT]
         |                     |                      |
    Omni Antenna          Directional           Omni Antenna
         |<------ 38cm ------->|<------ 38cm -------->|
                               |
                           [WEBCAM]
                        (reference imagery)
```

### Components

| Device | Role | Sample Rate | Notes |
|--------|------|-------------|-------|
| RTL-SDR x2 | Surveillance channels | 2.56 MSPS | Omnidirectional antennas, 38cm baseline |
| HackRF One | Reference channel | 8 MSPS | Log-periodic directional antenna |
| Webcam | Visual ground truth | 30 fps | 1280x720 MJPEG |

### Passive Radar Concept

1. **Reference signal**: Direct path from WiFi transmitters (HackRF with directional antenna)
2. **Surveillance signals**: Reflections from objects (RTL-SDRs with omni antennas)
3. **Cross-correlation**: Reveals range and Doppler of reflecting targets
4. **Angle estimation**: Phase difference between RTL-SDR pair (38cm baseline)

## Installation

```bash
cd ~/Projects/wifi-camera
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Dependencies

- numpy, scipy (signal processing)
- pyrtlsdr (optional, for direct SDR access)
- opencv-python (optional, for advanced frame processing)
- ffmpeg (required for webcam capture)

System tools required: `rtl_sdr`, `hackrf_transfer`, `ffmpeg`

## Usage

### Quick Start

```bash
cd ~/Projects/wifi-camera
source venv/bin/activate

# Basic 10-second capture
python capture.py

# 5-minute capture
python capture.py --duration 300

# Custom channel (1, 6, or 11 recommended)
python capture.py --duration 60 --channel 1

# Without HackRF or webcam
python capture.py --no-hackrf
python capture.py --no-webcam
```

### Monitoring

```bash
# In a separate terminal
python monitor.py --live      # Continuous monitoring
python monitor.py --devices   # Check device status
python monitor.py             # Single status snapshot
```

### Data Validation

```bash
python sanity_check.py data/<session_id>
```

### Spectrum Sweep

Synchronized full-band capture across all SDRs (25 MHz - 6 GHz):

```bash
# Quick test (100-500 MHz, 10 MHz steps) ~1 minute
./spectrum_sweep.py --quick

# Full spectrum (25 MHz - 6 GHz, 2 MHz steps) ~2 hours
./spectrum_sweep.py --full

# Custom range
./spectrum_sweep.py --start 400 --end 1000 --step 5

# WiFi 2.4 GHz band
./spectrum_sweep.py --start 2400 --end 2500 --step 1
```

#### Async Sweep (Faster)

The async sweep runs all devices in parallel continuously:
- RTL-SDRs loop through their range (25 MHz - 1.75 GHz) multiple times
- HackRF does a single sweep through full range (25 MHz - 6 GHz)
- ~7-8 minutes total vs ~2 hours for lockstep mode

```bash
# Full async sweep
./spectrum_sweep_async.py --full

# Quick async test
./spectrum_sweep_async.py --quick

# Custom settings
./spectrum_sweep_async.py --rtl-end 1000 --hackrf-end 3000 --rtl-step 5
```

**Output:** Per-frequency IQ files + video frames with timestamp correlation.

**Note:** RTL-SDRs only cover 25-1750 MHz; HackRF covers full range to 6 GHz.

## Output Structure

```
data/<session_id>/
├── frames/
│   ├── frame_000000_1763938203.738430.jpg
│   ├── frame_000001_1763938203.771234.jpg
│   └── ... (30 fps, timestamped)
├── frame_timestamps.json    # Per-frame timestamps
├── timing.json              # Per-device synchronization
├── metadata.json            # Capture configuration
├── rtlsdr_left.bin          # LEFT surveillance channel (uint8 IQ)
├── rtlsdr_right.bin         # RIGHT surveillance channel (uint8 IQ)
├── hackrf.bin               # Reference channel (int8 IQ)
└── DATA_FORMAT.md           # Data loading documentation
```

## Data Formats

### RTL-SDR (unsigned 8-bit IQ)

```python
import numpy as np

def load_rtlsdr(filepath, num_samples=-1):
    count = num_samples * 2 if num_samples > 0 else -1
    raw = np.fromfile(filepath, dtype=np.uint8, count=count)
    iq = (raw[0::2].astype(np.float32) - 127.5) / 127.5 + \
         1j * (raw[1::2].astype(np.float32) - 127.5) / 127.5
    return iq
```

### HackRF (signed 8-bit IQ)

```python
def load_hackrf(filepath, num_samples=-1):
    count = num_samples * 2 if num_samples > 0 else -1
    raw = np.fromfile(filepath, dtype=np.int8, count=count)
    iq = raw[0::2].astype(np.float32) / 128.0 + \
         1j * raw[1::2].astype(np.float32) / 128.0
    return iq
```

### Frame-to-IQ Correlation

```python
import json

with open('timing.json') as f:
    timing = json.load(f)

with open('frame_timestamps.json') as f:
    frames = json.load(f)

# Get IQ sample index for frame 100
frame_time = frames['frames'][100]['timestamp']
rtl_start = timing['streams']['rtlsdr_right']['first_data_time']
sample_rate = timing['sample_rates']['rtlsdr']

sample_index = int((frame_time - rtl_start) * sample_rate)
```

## Time Synchronization

All devices use system `time.time()` for timestamps:

- **timing.json**: Records `first_data_time` for each device (when first bytes received)
- **frame_timestamps.json**: Per-frame timestamps
- **Coordinated start**: SDR processes use barrier synchronization for tighter timing
- **Post-capture alignment**: Use `sync.py` for cross-correlation based alignment

### Synchronization Analysis

```bash
# Analyze synchronization quality for a capture session
python sync.py data/<session_id>
```

This provides:
- **Sample loss detection** - Validates sample counts match expected
- **Cross-correlation alignment** - More accurate than timestamp-based (typically <10ms)
- **Clock drift measurement** - Relative PPM drift between RTL-SDRs (~10-50 ppm typical)

### Timing Data

```json
{
  "streams": {
    "hackrf":       {"first_data_time": 1763938202.997},
    "webcam":       {"first_data_time": 1763938203.738},
    "rtlsdr_left":  {"first_data_time": 1763938203.902},
    "rtlsdr_right": {"first_data_time": 1763938203.848}
  },
  "sample_loss_percent": {
    "rtlsdr_left": -0.18,
    "rtlsdr_right": -0.39,
    "hackrf": -0.50
  }
}
```

**Note**: `first_data_time` reflects when Python received data (USB buffering), not when ADC sampling began. Cross-correlation provides more accurate alignment.

## Data Rates

| Device | Data Rate | 5-min Capture |
|--------|-----------|---------------|
| RTL-SDR (each) | ~4.8 MB/s | ~1.4 GB |
| HackRF | ~10 MB/s | ~3 GB |
| Webcam | ~5 MB/s | ~2 GB |
| **Total** | **~25 MB/s** | **~8 GB** |

## Physical Parameters

| Parameter | Value |
|-----------|-------|
| Center Frequency | 2437 MHz (Channel 6) |
| Wavelength | 12.5 cm |
| Antenna Baseline | 38 cm (~3 wavelengths) |
| Range Resolution (RTL-SDR) | 62.5 m |
| Range Resolution (HackRF) | 15 m |
| Speed of Light | 299,792,458 m/s |

## Files

| File | Description |
|------|-------------|
| `capture.py` | Main synchronized capture script |
| `monitor.py` | Live monitoring tool |
| `sanity_check.py` | Data validation and quality check |
| `process.py` | Signal processing algorithms |
| `spectrum_sweep.py` | Lockstep multi-SDR spectrum sweep |
| `spectrum_sweep_async.py` | Async parallel spectrum sweep (faster) |
| `sync.py` | Synchronization analysis and alignment |
| `gps.py` | GPS time and position reading |
| `devices.py` | Hardware detection and identification |
| `config.py` | Configuration classes |

## GPU Processing (CUDA)

For processing on GPU (e.g., RTX 4090):

```python
import cupy as cp
from cupyx.scipy import signal as cusignal

# Load to GPU
iq_gpu = cp.asarray(iq_data)

# FFT on GPU
spectrum = cp.fft.fft(iq_gpu)

# Cross-correlation
corr = cusignal.correlate(ref_gpu, surv_gpu, mode='full')
```

## Research Goals

1. **Verify passive radar** - Detect correlation peaks between reference and surveillance
2. **Moving target detection** - Doppler shifts from walking humans
3. **Range estimation** - Time delay of reflections
4. **Angle of arrival** - Phase difference with 38cm baseline
5. **Crude imaging** - Correlate RF patterns with video
6. **Material detection** - Reflection characteristics (speculative)

## Known Issues

- RTL-SDR devices have identical serial numbers (differentiated by USB path)
- Mismatched antennas cause weak cross-correlation (matched pair recommended)
- HackRF uses signed int8 (different from RTL-SDR unsigned int8)
- GPS provides time offset measurement (u-blox 7 receiver supported)
- **RTL-SDR frequency limitation**: NESDR SMArt v5 max frequency is 1.75 GHz (cannot receive 2.4 GHz WiFi without downconverters)
- **RTL-SDR thermal throttling**: Extended captures (>30 min) can cause overheating and device crashes. Add heatsinks and/or cooling breaks for long spectrum sweeps

## 915 MHz Bistatic Radar Subsystem

Due to RTL-SDR hardware limitations (1.75 GHz max), a separate 915 MHz bistatic radar system has been developed for testing passive radar techniques.

**Location:** `radar_test_915mhz/`

**Configuration:**
```
[RTL-SDR LEFT] <--38cm--> [HackRF TX] <--38cm--> [RTL-SDR RIGHT]
                            [WEBCAM]
```

**Key Differences from Main System:**
- **Frequency:** 915 MHz ISM band (legal unlicensed transmission)
- **HackRF Role:** Transmits CW beacon (not receiving)
- **RTL-SDRs:** Both act as surveillance receivers
- **Same Infrastructure:** Uses identical timing/sync code as main wifi-camera

**Quick Start:**
```bash
cd radar_test_915mhz
./capture_bistatic_915.py --duration 300  # 5-minute capture
```

**Validation:** Successfully tested with 5-minute capture showing perfect synchronization (1.00 correlation confidence, <0.1% sample loss). See `radar_test_915mhz/README.md` for complete documentation.

**Purpose:** Proof-of-concept for passive radar processing pipeline before obtaining 2.4 GHz downconverters for WiFi passive radar.

## Status

**EXPERIMENTAL** - Active development

Initial 5-minute captures successful with:
- Stable data rates (~45 MB/s sustained)
- 8,984 frames at 29.9 fps
- Per-device timestamps synchronized
- SNR: 21-46 dB across devices

## Related Projects

- [rf-to-image](https://github.com/bshepp/rf-to-image) - ML training pipeline for RF-to-image generation

## License

MIT License - See LICENSE file for details.
