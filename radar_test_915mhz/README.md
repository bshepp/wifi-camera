# 915 MHz Bistatic Radar Capture

Synchronized capture system for passive bistatic radar using RTL-SDRs at 915 MHz.

## Overview

This capture system provides **proper time synchronization** for passive radar processing:

- **Barrier synchronization** - All devices start simultaneously
- **Microsecond timestamps** - `first_data_time` for each stream
- **Sample counting** - Detect USB dropouts
- **Frame timestamps** - Video correlation
- **Metadata** - Complete capture configuration

## Hardware Setup

```
[RTL-SDR LEFT] <--38cm--> [HackRF TX] <--38cm--> [RTL-SDR RIGHT]
                            [WEBCAM]
```

**Required:**
- 2x RTL-SDR dongles (left/right positions)
- 1x HackRF One (transmitting beacon)
- 1x Webcam (optional, for ground truth)
- `beacon_900mhz_cw.iq` beacon file

**USB Port Assignment:**
RTL-SDRs must be in consistent USB ports (defined in `devices.py` `RTLSDR_POSITIONS`):
- Left: Bus 1, Port 4.2
- Right: Bus 1, Port 4.3

## Usage

### Basic Capture (30 seconds)
```bash
cd /home/tempest/Projects/wifi-camera/radar_test_915mhz
./capture_bistatic_915.py
```

### Custom Duration
```bash
./capture_bistatic_915.py --duration 60  # 60 seconds
```

### Different Frequency
```bash
./capture_bistatic_915.py --frequency 915000000  # 915 MHz (default)
./capture_bistatic_915.py --frequency 902000000  # 902 MHz (ISM band edge)
```

### Custom Output Directory
```bash
./capture_bistatic_915.py --output-dir /path/to/output --duration 300
```

## Output Structure

Each capture creates:
```
data/YYYYMMDD_HHMMSS/
├── rtlsdr_left.bin          # Left RTL-SDR IQ (uint8, 2.4 MSPS)
├── rtlsdr_right.bin         # Right RTL-SDR IQ (uint8, 2.4 MSPS)
├── frames/                  # Webcam frames (JPEG, ~30fps)
│   └── frame_NNNNNN.jpg
├── metadata.json            # Capture configuration
├── timing.json              # CRITICAL: Per-stream first_data_time
└── frame_timestamps.json    # Per-frame timestamps
```

## Timing Synchronization

**timing.json** contains critical timing data:
```json
{
  "capture_start_time": 1764795649.285043,
  "capture_stop_time": 1764795949.472955,
  "streams": {
    "rtlsdr_left": {
      "first_data_time": 1764795650.102933,  # When first bytes arrived
      "samples_written": 718274560,
      "bytes_written": 1436549120,
      "sample_loss_percent": 0.01
    },
    "rtlsdr_right": {
      "first_data_time": 1764795650.150865,
      "samples_written": 718667776,
      "bytes_written": 1437335552,
      "sample_loss_percent": -0.06
    }
  }
}
```

**Time Alignment:**
- `first_data_time` is when USB buffer delivered first samples
- Typically 10-50ms offset between RTL-SDRs (barrier sync + USB latency)
- Use `sync.py` for cross-correlation based sub-microsecond alignment

## Post-Processing

### Validate Capture Quality
```bash
cd /home/tempest/Projects/wifi-camera
python sanity_check.py radar_test_915mhz/data/YYYYMMDD_HHMMSS
```

### Analyze Synchronization
```bash
python sync.py radar_test_915mhz/data/YYYYMMDD_HHMMSS
```

### Load IQ Data for Processing
```python
import sys
sys.path.append('/home/tempest/Projects/wifi-camera')
from process import load_rtlsdr_iq
import json

# Load timing data
with open('data/YYYYMMDD_HHMMSS/timing.json') as f:
    timing = json.load(f)

# Load IQ samples
iq_left = load_rtlsdr_iq('data/YYYYMMDD_HHMMSS/rtlsdr_left.bin')
iq_right = load_rtlsdr_iq('data/YYYYMMDD_HHMMSS/rtlsdr_right.bin')

# Calculate time offset between channels
left_start = timing['streams']['rtlsdr_left']['first_data_time']
right_start = timing['streams']['rtlsdr_right']['first_data_time']
time_offset_ms = (right_start - left_start) * 1000
print(f"RTL-SDR time offset: {time_offset_ms:.2f} ms")

# Convert to sample offset
sample_rate = timing['sample_rates']['rtlsdr']
sample_offset = int((right_start - left_start) * sample_rate)
print(f"Sample offset: {sample_offset} samples")
```

## Differences from wifi-camera/capture.py

**Simplified for Bistatic:**
- Only 2 RTL-SDR receivers (no HackRF RX)
- HackRF transmits beacon in background
- Same timing infrastructure as wifi-camera
- No GPS integration (yet)

**Key Features Preserved:**
- ✓ Barrier synchronization
- ✓ `first_data_time` timestamps
- ✓ Sample loss detection
- ✓ Frame timestamps
- ✓ Metadata generation

## Beacon Generation

The beacon file `beacon_900mhz_cw.iq` is a simple CW (continuous wave) signal:
```python
import numpy as np
sample_rate = 10e6  # 10 MSPS
duration = 1.0  # 1 second (loops)
i_samples = np.full(int(sample_rate * duration), 100, dtype=np.int8)
q_samples = np.zeros(int(sample_rate * duration), dtype=np.int8)
iq_data = np.empty(len(i_samples) * 2, dtype=np.int8)
iq_data[0::2] = i_samples
iq_data[1::2] = q_samples
iq_data.tofile('beacon_900mhz_cw.iq')
```

## Troubleshooting

**"ERROR: Need 2 RTL-SDR devices"**
- Check `rtl_test` shows 2 devices
- Verify USB connections

**"ERROR: Could not identify left/right positions"**
- RTL-SDRs in wrong USB ports
- Check USB paths in `devices.py` `RTLSDR_POSITIONS`

**"ERROR: Beacon file not found"**
- Run from `radar_test_915mhz/` directory
- Or specify `--beacon-file /full/path/to/beacon_900mhz_cw.iq`

**High sample loss**
- Reduce capture duration
- Close other applications
- Check USB bandwidth (avoid USB hubs)

## Data Rates

**30-second capture:**
- RTL-SDR Left: 138 MB (2.4 MSPS * 30s)
- RTL-SDR Right: 138 MB
- Webcam: ~84 MB (900 frames @ 30fps)
- **Total: ~360 MB**

**Sustained rate:** ~12 MB/s (manageable on modern systems)

## Next Steps

1. **Validate** with `sanity_check.py` - Check signal quality
2. **Analyze** with `sync.py` - Measure cross-correlation alignment
3. **Process** with `process.py` - Generate range-Doppler maps
4. **External beacon** - Free HackRF for reference channel (proper passive radar)

## Validation Results

### 5-Minute Test Capture (Session: 20251204_211702)

**Capture Quality:**
```
Capture duration: 302.1s (5 minutes)
Sample rate: 2.4 MSPS

Streams:
  ✓ rtlsdr_left:  722,993,152 samples (301.2s) - loss: -0.02% ✓
  ✓ rtlsdr_right: 723,255,296 samples (301.4s) - loss: -0.06% ✓
  ✓ webcam:       9,000 frames @ 29.8 fps
```

**Synchronization Analysis:**
```
Alignment (cross-correlation):
  rtlsdr_left vs rtlsdr_right:
    Offset: +40,120 samples (+16.72 ms)
    Confidence: 1.00 (perfect correlation!)

Clock Drift:
  rtlsdr_left vs rtlsdr_right:
    Drift: +0.76 ppm (+1.8 samples/s)
```

**Assessment:**
- ✓ Excellent sample retention (<0.1% loss on both channels)
- ✓ Perfect cross-correlation confidence (1.00)
- ✓ Minimal clock drift (0.76 ppm)
- ✓ Stable 30 fps video capture
- ✓ Complete timing metadata generated

**Conclusion:** The synchronized capture system is working perfectly. Data is ready for passive radar processing.

**Test Date:** December 4, 2025

## See Also

- `../capture.py` - Original wifi-camera synchronized capture
- `../sync.py` - Cross-correlation alignment analysis
- `../process.py` - Signal processing functions
- `../devices.py` - Device detection and USB path mapping
