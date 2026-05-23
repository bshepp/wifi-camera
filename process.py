#!/usr/bin/env python3
"""
WiFi Camera - Signal Processing

Passive radar processing:
- Cross-correlation between reference and surveillance channels
- Range-Doppler processing
- Phase difference for angle estimation
- Time alignment using correlation
"""

import numpy as np
from pathlib import Path
from typing import Tuple, Optional, Dict, List
from dataclasses import dataclass
from math import gcd
import json

# Constants
SPEED_OF_LIGHT = 299_792_458  # m/s


def resample_iq(iq: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Polyphase-resample complex IQ from src_rate to dst_rate.

    For default HackRF→RTL-SDR (8 MSPS → 2.56 MSPS) this is up-8 / down-25.
    Uses scipy.signal.resample_poly (which handles complex input natively
    and applies the appropriate anti-aliasing filter).
    """
    if src_rate == dst_rate:
        return iq
    # Imported lazily so capture-only environments don't need scipy.
    from scipy.signal import resample_poly
    g = gcd(src_rate, dst_rate)
    up = dst_rate // g
    down = src_rate // g
    return resample_poly(iq, up, down).astype(np.complex64)


@dataclass
class ProcessingParams:
    """Parameters for signal processing"""
    # Sample rates
    rtlsdr_sample_rate: int = 2_560_000
    hackrf_sample_rate: int = 8_000_000

    # Physical parameters
    center_frequency: float = 2.437e9  # Hz (Channel 6)
    baseline_m: float = 0.38  # Distance between RTL-SDR antennas

    # Processing windows
    range_window_samples: int = 4096  # Samples per range window
    doppler_fft_size: int = 256  # Number of range windows for Doppler FFT

    # Detection
    cfar_guard_cells: int = 4
    cfar_training_cells: int = 16
    cfar_threshold_factor: float = 10.0  # dB above noise floor

    @property
    def wavelength(self) -> float:
        """Wavelength in meters"""
        return SPEED_OF_LIGHT / self.center_frequency

    @property
    def range_resolution_rtlsdr(self) -> float:
        """Range resolution for RTL-SDR in meters"""
        return SPEED_OF_LIGHT / (2 * self.rtlsdr_sample_rate)

    @property
    def max_unambiguous_range_rtlsdr(self) -> float:
        """Maximum unambiguous range for RTL-SDR"""
        return self.range_window_samples * self.range_resolution_rtlsdr / 2


def load_iq_data(filepath: Path, signed: bool = False,
                 offset: int = 0, count: int = -1) -> np.ndarray:
    """
    Load IQ data from binary file.

    RTL-SDR uses unsigned 8-bit IQ data (uint8, center at 127.5)
    HackRF uses signed 8-bit IQ data (int8, center at 0)
    Format: I0, Q0, I1, Q1, ...

    Args:
        filepath: Path to binary IQ file
        signed: True for HackRF (int8), False for RTL-SDR (uint8)
        offset: Byte offset to start reading
        count: Number of bytes to read (-1 for all)

    Returns complex64 array normalized to [-1, 1] range.
    """
    if signed:
        # HackRF: signed 8-bit
        raw = np.fromfile(filepath, dtype=np.int8, offset=offset, count=count)
        raw = raw.reshape(-1, 2)
        iq = raw[:, 0].astype(np.float32) / 128.0 + \
             1j * raw[:, 1].astype(np.float32) / 128.0
    else:
        # RTL-SDR: unsigned 8-bit
        raw = np.fromfile(filepath, dtype=np.uint8, offset=offset, count=count)
        raw = raw.reshape(-1, 2)
        iq = (raw[:, 0].astype(np.float32) - 127.5) / 127.5 + \
             1j * (raw[:, 1].astype(np.float32) - 127.5) / 127.5

    return iq


def load_rtlsdr_iq(filepath: Path, samples: int = -1) -> np.ndarray:
    """Load RTL-SDR IQ data (unsigned 8-bit)"""
    count = samples * 2 if samples > 0 else -1
    return load_iq_data(filepath, signed=False, count=count)


def load_hackrf_iq(filepath: Path, samples: int = -1) -> np.ndarray:
    """Load HackRF IQ data (signed 8-bit)"""
    count = samples * 2 if samples > 0 else -1
    return load_iq_data(filepath, signed=True, count=count)


def cross_correlation(reference: np.ndarray, surveillance: np.ndarray,
                      max_lag: int = None) -> np.ndarray:
    """
    Compute cross-correlation between reference and surveillance signals.

    For passive radar:
    - reference: direct path signal (from HackRF with directional antenna)
    - surveillance: reflected signals (from RTL-SDR)

    Returns complex correlation values for each lag (range bin).
    """
    if max_lag is None:
        max_lag = len(surveillance) // 2

    # Use FFT-based correlation for efficiency
    n = len(reference) + max_lag
    n_fft = int(2 ** np.ceil(np.log2(n)))

    ref_fft = np.fft.fft(reference, n_fft)
    surv_fft = np.fft.fft(surveillance, n_fft)

    # Cross-correlation via FFT
    corr = np.fft.ifft(surv_fft * np.conj(ref_fft))

    # Return positive lags only (causal reflections)
    return corr[:max_lag]


def range_doppler_map(reference: np.ndarray, surveillance: np.ndarray,
                      params: ProcessingParams) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Compute range-Doppler map using batch processing.

    Returns:
        rdm: 2D array of range-Doppler map (magnitude in dB)
        ranges: 1D array of range bins in meters
        dopplers: 1D array of Doppler bins in Hz
    """
    n_samples = min(len(reference), len(surveillance))
    window_size = params.range_window_samples
    n_windows = params.doppler_fft_size

    # Ensure we have enough samples
    required_samples = window_size * n_windows
    if n_samples < required_samples:
        raise ValueError(f"Need {required_samples} samples, only have {n_samples}")

    # Truncate to exact multiple
    n_samples = window_size * n_windows
    reference = reference[:n_samples]
    surveillance = surveillance[:n_samples]

    # Reshape into windows
    ref_windows = reference.reshape(n_windows, window_size)
    surv_windows = surveillance.reshape(n_windows, window_size)

    # Cross-correlation for each window (range processing)
    range_profiles = np.zeros((n_windows, window_size), dtype=np.complex64)
    for i in range(n_windows):
        # Normalize reference for each window
        ref_norm = ref_windows[i] / (np.abs(ref_windows[i]).max() + 1e-10)
        range_profiles[i] = cross_correlation(ref_norm, surv_windows[i], window_size)

    # Doppler processing (FFT across windows for each range bin)
    # Apply window function to reduce sidelobes
    doppler_window = np.hanning(n_windows)
    range_profiles *= doppler_window[:, np.newaxis]

    rdm = np.fft.fftshift(np.fft.fft(range_profiles, axis=0), axes=0)

    # Convert to dB
    rdm_db = 20 * np.log10(np.abs(rdm) + 1e-10)

    # Compute axis values
    range_resolution = SPEED_OF_LIGHT / (2 * params.rtlsdr_sample_rate)
    ranges = np.arange(window_size) * range_resolution

    # Doppler resolution
    pri = window_size / params.rtlsdr_sample_rate  # Pulse repetition interval
    doppler_resolution = 1 / (n_windows * pri)
    dopplers = np.fft.fftshift(np.fft.fftfreq(n_windows, pri))

    return rdm_db, ranges, dopplers


def estimate_time_offset(sig1: np.ndarray, sig2: np.ndarray,
                         max_offset_samples: int = 10000) -> int:
    """
    Estimate time offset between two signals using cross-correlation.

    Useful for aligning RTL-SDR captures that started at slightly different times.

    Returns offset in samples (positive means sig2 is ahead of sig1).
    """
    # Use a subset for efficiency
    n = min(len(sig1), len(sig2), 100000)
    s1 = sig1[:n]
    s2 = sig2[:n]

    # Cross-correlation
    corr = np.correlate(np.abs(s1), np.abs(s2), mode='full')

    # Find peak
    center = len(corr) // 2
    search_range = min(max_offset_samples, center)
    search_region = corr[center - search_range:center + search_range]

    offset = np.argmax(search_region) - search_range

    return offset


def phase_difference(left: np.ndarray, right: np.ndarray,
                     window_size: int = 1024) -> np.ndarray:
    """
    Compute phase difference between left and right RTL-SDR channels.

    For angle-of-arrival estimation using the 38cm baseline.
    """
    n_windows = min(len(left), len(right)) // window_size

    phase_diffs = np.zeros(n_windows)

    for i in range(n_windows):
        start = i * window_size
        end = start + window_size

        l_window = left[start:end]
        r_window = right[start:end]

        # Cross-correlation at zero lag
        cross = np.sum(l_window * np.conj(r_window))

        # Phase difference
        phase_diffs[i] = np.angle(cross)

    return phase_diffs


def angle_from_phase(phase_diff: float, baseline_m: float,
                     wavelength_m: float) -> float:
    """
    Convert phase difference to angle of arrival.

    phase_diff: Phase difference in radians
    baseline_m: Distance between antennas in meters
    wavelength_m: Wavelength in meters

    Returns angle in degrees from broadside (0 = perpendicular to baseline).

    Note: There is ambiguity when baseline > wavelength/2.
    With 38cm baseline at 2.4GHz (12.5cm wavelength), we have ~3 wavelengths,
    so there will be grating lobes / ambiguity.
    """
    # sin(theta) = (phase_diff * wavelength) / (2 * pi * baseline)
    sin_theta = (phase_diff * wavelength_m) / (2 * np.pi * baseline_m)

    # Clamp to valid range
    sin_theta = np.clip(sin_theta, -1, 1)

    return np.degrees(np.arcsin(sin_theta))


def cfar_detector(data: np.ndarray, guard_cells: int = 4,
                  training_cells: int = 16, threshold_factor: float = 10.0,
                  axis: int = None) -> np.ndarray:
    """
    Constant False Alarm Rate (CFAR) detector.

    Returns boolean mask of detected targets.
    """
    if axis is None:
        # 1D CFAR
        n = len(data)
        detections = np.zeros(n, dtype=bool)
        total_cells = guard_cells + training_cells

        for i in range(total_cells, n - total_cells):
            # Training cells on each side
            left_train = data[i - total_cells:i - guard_cells]
            right_train = data[i + guard_cells + 1:i + total_cells + 1]

            # Estimate noise floor
            noise_floor = np.mean(np.concatenate([left_train, right_train]))

            # Threshold
            threshold = noise_floor * threshold_factor

            if data[i] > threshold:
                detections[i] = True

        return detections
    else:
        # 2D CFAR (apply 1D along specified axis)
        return np.apply_along_axis(
            lambda x: cfar_detector(x, guard_cells, training_cells, threshold_factor),
            axis, data
        )


class WiFiCameraProcessor:
    """Main processing class for WiFi Camera data"""

    def __init__(self, session_dir: Path, params: ProcessingParams = None):
        self.session_dir = Path(session_dir)
        self.params = params or ProcessingParams()

        # Load metadata
        metadata_file = self.session_dir / "metadata.json"
        if metadata_file.exists():
            with open(metadata_file) as f:
                self.metadata = json.load(f)

            # Update params from metadata
            if "frequency_hz" in self.metadata:
                self.params.center_frequency = self.metadata["frequency_hz"]
            if "rtlsdr_sample_rate" in self.metadata:
                self.params.rtlsdr_sample_rate = self.metadata["rtlsdr_sample_rate"]
            if "hackrf_sample_rate" in self.metadata:
                self.params.hackrf_sample_rate = self.metadata["hackrf_sample_rate"]
        else:
            self.metadata = {}

        # Data arrays (loaded on demand)
        self._rtlsdr_left: Optional[np.ndarray] = None
        self._rtlsdr_right: Optional[np.ndarray] = None
        self._hackrf: Optional[np.ndarray] = None

    def load_rtlsdr_left(self, samples: int = -1) -> np.ndarray:
        """Load left RTL-SDR data (unsigned 8-bit format).

        Only the full-file load is cached. Partial loads (samples > 0) always
        re-read from disk to avoid returning a previously-cached short array
        when the caller asks for the full file later.
        """
        if samples > 0:
            filepath = self.session_dir / "rtlsdr_left.bin"
            return load_rtlsdr_iq(filepath, samples=samples)
        if self._rtlsdr_left is None:
            filepath = self.session_dir / "rtlsdr_left.bin"
            self._rtlsdr_left = load_rtlsdr_iq(filepath, samples=-1)
        return self._rtlsdr_left

    def load_rtlsdr_right(self, samples: int = -1) -> np.ndarray:
        """Load right RTL-SDR data (unsigned 8-bit format). See load_rtlsdr_left."""
        if samples > 0:
            filepath = self.session_dir / "rtlsdr_right.bin"
            return load_rtlsdr_iq(filepath, samples=samples)
        if self._rtlsdr_right is None:
            filepath = self.session_dir / "rtlsdr_right.bin"
            self._rtlsdr_right = load_rtlsdr_iq(filepath, samples=-1)
        return self._rtlsdr_right

    def load_hackrf(self, samples: int = -1) -> np.ndarray:
        """Load HackRF data (signed 8-bit format). See load_rtlsdr_left."""
        if samples > 0:
            filepath = self.session_dir / "hackrf.bin"
            return load_hackrf_iq(filepath, samples=samples)
        if self._hackrf is None:
            filepath = self.session_dir / "hackrf.bin"
            self._hackrf = load_hackrf_iq(filepath, samples=-1)
        return self._hackrf

    def align_rtlsdr_channels(self) -> int:
        """
        Align left and right RTL-SDR channels.

        Returns offset applied (in samples).
        """
        left = self.load_rtlsdr_left(samples=100000)
        right = self.load_rtlsdr_right(samples=100000)

        offset = estimate_time_offset(left, right)
        print(f"Time offset between RTL-SDRs: {offset} samples "
              f"({offset / self.params.rtlsdr_sample_rate * 1e6:.1f} us)")

        return offset

    def compute_range_doppler(self, use_hackrf_reference: bool = True,
                              surveillance_channel: str = "left"
                              ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Compute range-Doppler map.

        Args:
            use_hackrf_reference: If True, use HackRF as reference, polyphase-
                                  resampled to the RTL-SDR rate so both arms
                                  of the correlation share a common timebase.
                                  If False, use the other RTL-SDR as reference.
            surveillance_channel: "left" or "right" RTL-SDR for surveillance.
        """
        # Load surveillance channel
        if surveillance_channel == "left":
            surveillance = self.load_rtlsdr_left()
        else:
            surveillance = self.load_rtlsdr_right()

        if use_hackrf_reference:
            reference = self.load_hackrf()
            if self.params.hackrf_sample_rate != self.params.rtlsdr_sample_rate:
                reference = resample_iq(
                    reference,
                    self.params.hackrf_sample_rate,
                    self.params.rtlsdr_sample_rate,
                )
        else:
            # Use the other RTL-SDR as reference (already on the same rate)
            if surveillance_channel == "left":
                reference = self.load_rtlsdr_right()
            else:
                reference = self.load_rtlsdr_left()

        return range_doppler_map(reference, surveillance, self.params)

    def compute_angle_of_arrival(self, window_size: int = 1024) -> np.ndarray:
        """
        Compute angle of arrival over time using phase difference.
        """
        left = self.load_rtlsdr_left()
        right = self.load_rtlsdr_right()

        # Align first
        offset = estimate_time_offset(left, right)
        if offset > 0:
            left = left[offset:]
        elif offset < 0:
            right = right[-offset:]

        # Truncate to same length
        n = min(len(left), len(right))
        left = left[:n]
        right = right[:n]

        # Compute phase difference
        phase_diffs = phase_difference(left, right, window_size)

        # Convert to angles
        angles = np.array([
            angle_from_phase(pd, self.params.baseline_m, self.params.wavelength)
            for pd in phase_diffs
        ])

        return angles


def main():
    """Test processing with a captured session"""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python process.py <session_dir>")
        print("Example: python process.py data/20231115_143022")
        sys.exit(1)

    session_dir = Path(sys.argv[1])
    if not session_dir.exists():
        print(f"Session directory not found: {session_dir}")
        sys.exit(1)

    processor = WiFiCameraProcessor(session_dir)

    print(f"Processing session: {session_dir.name}")
    print(f"Center frequency: {processor.params.center_frequency / 1e9:.3f} GHz")
    print(f"Wavelength: {processor.params.wavelength * 100:.1f} cm")
    print(f"Range resolution: {processor.params.range_resolution_rtlsdr:.1f} m")

    # Align channels
    print("\nAligning RTL-SDR channels...")
    offset = processor.align_rtlsdr_channels()

    # Compute angle of arrival
    print("\nComputing angle of arrival...")
    angles = processor.compute_angle_of_arrival()
    print(f"Mean angle: {np.mean(angles):.1f} degrees")
    print(f"Std angle: {np.std(angles):.1f} degrees")

    print("\nProcessing complete!")


if __name__ == "__main__":
    main()
