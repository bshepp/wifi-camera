#!/usr/bin/env python3
"""
WiFi Camera - Data Sanity Check

Validates captured IQ data and provides diagnostic information.

Usage:
    python sanity_check.py <session_dir>
    python sanity_check.py data/20251123_210344
"""

import sys
import json
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional


# ANSI colors
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    DIM = "\033[2m"


@dataclass
class IQStats:
    """Statistics for IQ data"""
    name: str
    file_size: int
    total_samples: int
    i_mean: float
    i_std: float
    q_mean: float
    q_std: float
    magnitude_mean: float
    power_db: float
    noise_floor_db: float
    peak_db: float
    snr_db: float
    power_variation_pct: float
    issues: List[str]
    valid: bool


def load_rtlsdr_iq(filepath: Path, max_samples: int = 1000000) -> Tuple[np.ndarray, np.ndarray]:
    """Load RTL-SDR IQ data (unsigned 8-bit)"""
    raw = np.fromfile(filepath, dtype=np.uint8, count=max_samples * 2)
    raw = raw.reshape(-1, 2)
    iq = (raw[:, 0].astype(np.float32) - 127.5) / 127.5 + \
         1j * (raw[:, 1].astype(np.float32) - 127.5) / 127.5
    return iq, raw


def load_hackrf_iq(filepath: Path, max_samples: int = 1000000) -> Tuple[np.ndarray, np.ndarray]:
    """Load HackRF IQ data (signed 8-bit).

    Returns (complex IQ, raw int8 channel array). Raw values are in their
    native int8 range so I/Q mean prints near 0 for a healthy DC-centered
    signal — earlier this function reloaded the file as uint8 which made
    HackRF stats look like RTL-SDR (~127.5 mean) regardless of the actual
    DC offset.
    """
    raw = np.fromfile(filepath, dtype=np.int8, count=max_samples * 2)
    raw = raw.reshape(-1, 2)
    iq = raw[:, 0].astype(np.float32) / 128.0 + \
         1j * raw[:, 1].astype(np.float32) / 128.0
    return iq, raw


def compute_spectrum(iq: np.ndarray, fft_size: int = 8192) -> Tuple[float, float]:
    """Compute noise floor and peak from spectrum"""
    n_avg = min(10, len(iq) // fft_size)
    if n_avg == 0:
        return 0.0, 0.0

    spectrum = np.zeros(fft_size)
    for i in range(n_avg):
        chunk = iq[i * fft_size:(i + 1) * fft_size]
        spectrum += np.abs(np.fft.fftshift(np.fft.fft(chunk * np.hanning(fft_size)))) ** 2
    spectrum /= n_avg
    spectrum_db = 10 * np.log10(spectrum + 1e-10)

    noise_floor = np.median(spectrum_db)
    peak = np.max(spectrum_db)

    return noise_floor, peak


def analyze_iq_file(name: str, filepath: Path, is_hackrf: bool = False) -> IQStats:
    """Analyze an IQ file and return statistics"""
    issues = []

    # File info
    file_size = filepath.stat().st_size
    total_samples = file_size // 2

    # Load data
    if is_hackrf:
        iq, raw = load_hackrf_iq(filepath)
        expected_center = 0  # signed int8
        expected_range = 128
    else:
        iq, raw = load_rtlsdr_iq(filepath)
        expected_center = 127.5  # unsigned uint8
        expected_range = 127.5

    # Raw statistics
    i_mean = float(raw[:, 0].mean())
    i_std = float(raw[:, 0].std())
    q_mean = float(raw[:, 1].mean())
    q_std = float(raw[:, 1].std())

    # Check for issues. RTL-SDR (uint8) is centered at 127.5; HackRF (int8)
    # is centered at 0. The std-threshold is the same in either scale.
    if i_std < 1.0:
        issues.append("I channel very low variance")
    if q_std < 1.0:
        issues.append("Q channel very low variance")
    if abs(i_mean - expected_center) > 15:
        issues.append(f"I channel DC offset: {i_mean - expected_center:+.1f}")
    if abs(q_mean - expected_center) > 15:
        issues.append(f"Q channel DC offset: {q_mean - expected_center:+.1f}")

    # IQ statistics
    magnitude_mean = float(np.abs(iq).mean())
    power_db = float(10 * np.log10(np.mean(np.abs(iq) ** 2) + 1e-10))

    # Power variation (indicates signal activity)
    chunk_size = 10000
    n_chunks = min(100, len(iq) // chunk_size)
    if n_chunks > 1:
        chunk_powers = [np.mean(np.abs(iq[i * chunk_size:(i + 1) * chunk_size]) ** 2)
                        for i in range(n_chunks)]
        power_var = np.std(chunk_powers) / np.mean(chunk_powers) * 100 if np.mean(chunk_powers) > 0 else 0
    else:
        power_var = 0

    # Spectrum analysis
    noise_floor, peak = compute_spectrum(iq)
    snr = peak - noise_floor

    # Validity check
    valid = len(issues) == 0 and snr > 5 and i_std > 0.5 and q_std > 0.5

    return IQStats(
        name=name,
        file_size=file_size,
        total_samples=total_samples,
        i_mean=i_mean,
        i_std=i_std,
        q_mean=q_mean,
        q_std=q_std,
        magnitude_mean=magnitude_mean,
        power_db=power_db,
        noise_floor_db=noise_floor,
        peak_db=peak,
        snr_db=snr,
        power_variation_pct=power_var,
        issues=issues,
        valid=valid,
    )


def check_correlation(left_iq: np.ndarray, right_iq: np.ndarray) -> Dict:
    """Check correlation between left and right RTL-SDR channels.

    Uses up to 500k samples (~195 ms at 2.56 MSPS) via an FFT-based
    correlation rather than the previous 10k-sample O(n^2) np.correlate —
    enough to find the alignment peak even when the inter-stream offset
    is in the multi-millisecond range, while staying interactive.
    """
    n = min(500_000, len(left_iq), len(right_iq))
    left = left_iq[:n]
    right = right_iq[:n]

    # Time offset via FFT-based envelope correlation. Envelope (|IQ|) is
    # robust to the per-stream phase rotation that defeats raw IQ
    # correlation; FFT keeps us O(n log n).
    env_l = np.abs(left).astype(np.float32)
    env_r = np.abs(right).astype(np.float32)
    env_l -= env_l.mean()
    env_r -= env_r.mean()
    nfft = 1 << ((2 * n - 1).bit_length())
    L = np.fft.rfft(env_l, nfft)
    R = np.fft.rfft(env_r, nfft)
    xcorr = np.fft.irfft(L * np.conj(R), nfft)
    # Positive lags are in xcorr[:n], negative lags wrap around in xcorr[-n+1:]
    peak_pos = int(np.argmax(xcorr[:n]))
    peak_neg = int(np.argmax(xcorr[-(n - 1):]))
    if xcorr[peak_pos] >= xcorr[-(n - 1) + peak_neg]:
        offset = peak_pos
    else:
        offset = peak_neg - (n - 1)

    # Correlation coefficient on envelopes, computed over the full window
    # (was using the same 10k subset as the offset estimate above).
    corr_coef = float(np.corrcoef(np.abs(left), np.abs(right))[0, 1])

    # Phase coherence
    cross = left * np.conj(right)
    phase_diff_mean = float(np.angle(np.mean(cross)))
    phase_diff_std = float(np.std(np.angle(cross)))
    coherence = float(np.abs(np.mean(cross)) /
                     (np.sqrt(np.mean(np.abs(left) ** 2)) * np.sqrt(np.mean(np.abs(right) ** 2)) + 1e-10))

    return {
        "time_offset_samples": offset,
        "correlation_coefficient": corr_coef,
        "phase_diff_mean_deg": np.degrees(phase_diff_mean),
        "phase_diff_std_deg": np.degrees(phase_diff_std),
        "coherence": coherence,
    }


def print_stats(stats: IQStats):
    """Print statistics for an IQ file"""
    status = f"{C.GREEN}VALID{C.RESET}" if stats.valid else f"{C.YELLOW}CHECK{C.RESET}"

    print(f"\n{C.BOLD}{stats.name}{C.RESET} [{status}]")
    print(f"{'─' * 50}")
    print(f"  File size:     {stats.file_size / 1e6:.2f} MB ({stats.total_samples:,} samples)")
    print(f"  I channel:     mean={stats.i_mean:.1f}, std={stats.i_std:.1f}")
    print(f"  Q channel:     mean={stats.q_mean:.1f}, std={stats.q_std:.1f}")
    print(f"  Power:         {stats.power_db:.1f} dB")
    print(f"  Spectrum:      noise={stats.noise_floor_db:.1f} dB, peak={stats.peak_db:.1f} dB")
    print(f"  SNR:           {stats.snr_db:.1f} dB", end="")
    if stats.snr_db > 20:
        print(f" {C.GREEN}(excellent){C.RESET}")
    elif stats.snr_db > 10:
        print(f" {C.GREEN}(good){C.RESET}")
    elif stats.snr_db > 5:
        print(f" {C.YELLOW}(marginal){C.RESET}")
    else:
        print(f" {C.RED}(poor){C.RESET}")
    print(f"  Activity:      {stats.power_variation_pct:.1f}% power variation")

    if stats.issues:
        print(f"\n  {C.YELLOW}Issues:{C.RESET}")
        for issue in stats.issues:
            print(f"    - {issue}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <session_dir>")
        print(f"Example: python {sys.argv[0]} data/20251123_210344")
        sys.exit(1)

    session_dir = Path(sys.argv[1])
    if not session_dir.exists():
        print(f"{C.RED}Error: Session directory not found: {session_dir}{C.RESET}")
        sys.exit(1)

    print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}{'WiFi Camera - Data Sanity Check':^60}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")

    print(f"\n{C.BOLD}Session:{C.RESET} {session_dir.name}")

    # Load metadata if available
    metadata_file = session_dir / "metadata.json"
    if metadata_file.exists():
        with open(metadata_file) as f:
            metadata = json.load(f)
        freq_mhz = metadata.get("frequency_hz", 0) / 1e6
        channel = metadata.get("wifi_channel", "?")
        print(f"{C.DIM}Channel {channel} ({freq_mhz:.1f} MHz){C.RESET}")

    # Analyze each file
    all_stats = {}

    rtl_left_file = session_dir / "rtlsdr_left.bin"
    if rtl_left_file.exists():
        stats = analyze_iq_file("RTL-SDR LEFT", rtl_left_file, is_hackrf=False)
        all_stats["rtlsdr_left"] = stats
        print_stats(stats)

    rtl_right_file = session_dir / "rtlsdr_right.bin"
    if rtl_right_file.exists():
        stats = analyze_iq_file("RTL-SDR RIGHT", rtl_right_file, is_hackrf=False)
        all_stats["rtlsdr_right"] = stats
        print_stats(stats)

    hackrf_file = session_dir / "hackrf.bin"
    if hackrf_file.exists():
        stats = analyze_iq_file("HackRF", hackrf_file, is_hackrf=True)
        all_stats["hackrf"] = stats
        print_stats(stats)

    # Cross-correlation check between RTL-SDRs
    if "rtlsdr_left" in all_stats and "rtlsdr_right" in all_stats:
        print(f"\n{C.BOLD}Cross-Correlation (LEFT vs RIGHT){C.RESET}")
        print(f"{'─' * 50}")

        left_iq, _ = load_rtlsdr_iq(rtl_left_file)
        right_iq, _ = load_rtlsdr_iq(rtl_right_file)

        corr_stats = check_correlation(left_iq, right_iq)

        print(f"  Time offset:   {corr_stats['time_offset_samples']} samples")
        print(f"  Correlation:   {corr_stats['correlation_coefficient']:.3f}", end="")
        if corr_stats['correlation_coefficient'] > 0.5:
            print(f" {C.GREEN}(strong){C.RESET}")
        elif corr_stats['correlation_coefficient'] > 0.2:
            print(f" {C.YELLOW}(moderate){C.RESET}")
        else:
            print(f" {C.RED}(weak){C.RESET}")
        print(f"  Phase diff:    {corr_stats['phase_diff_mean_deg']:.1f} +/- {corr_stats['phase_diff_std_deg']:.1f} deg")
        print(f"  Coherence:     {corr_stats['coherence']:.3f}")

    # Webcam check
    webcam_file = session_dir / "webcam.mkv"
    if webcam_file.exists():
        print(f"\n{C.BOLD}Webcam{C.RESET}")
        print(f"{'─' * 50}")
        size = webcam_file.stat().st_size
        print(f"  File size:     {size / 1e6:.2f} MB")
        print(f"  Status:        {C.GREEN}Present{C.RESET}")

    # Summary
    print(f"\n{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")
    print(f"{C.BOLD}SUMMARY{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}{'═' * 60}{C.RESET}")

    all_valid = all(s.valid for s in all_stats.values())
    issues_found = sum(len(s.issues) for s in all_stats.values())

    if all_valid and issues_found == 0:
        print(f"{C.GREEN}All data files passed validation.{C.RESET}")
    else:
        print(f"{C.YELLOW}Found {issues_found} issue(s) across {len(all_stats)} files.{C.RESET}")
        if "rtlsdr_left" in all_stats and "rtlsdr_right" in all_stats:
            left_snr = all_stats["rtlsdr_left"].snr_db
            right_snr = all_stats["rtlsdr_right"].snr_db
            if abs(left_snr - right_snr) > 10:
                print(f"\n{C.YELLOW}Recommendation:{C.RESET}")
                print(f"  Large SNR difference between RTL-SDRs ({left_snr:.0f} vs {right_snr:.0f} dB)")
                print(f"  Check antenna connections and gain settings.")

    print()


if __name__ == "__main__":
    main()
