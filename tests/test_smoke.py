"""End-to-end smoke tests for the processing path.

Locks in behavior of the bug fixes from the December 2026 review:
- resample_iq direction/length
- apply_drift_correction sign (fast stream gets compressed)
- estimate_offset_correlation finds known offsets
- load_rtlsdr_iq / load_hackrf_iq normalize correctly
- WiFiCameraProcessor.compute_range_doppler produces sane output on real data

Run from the project root:
    source venv/bin/activate
    pytest tests/
"""

import sys
from pathlib import Path

import numpy as np
import pytest

# Make the repo root importable when tests are run as a package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from process import (
    WiFiCameraProcessor,
    load_rtlsdr_iq,
    load_hackrf_iq,
    resample_iq,
)
from sync import apply_drift_correction, estimate_offset_correlation


# ---------------------------------------------------------------------------
# Synthetic unit tests — fast, no external data needed
# ---------------------------------------------------------------------------

def test_resample_iq_preserves_tone_frequency():
    """A 100 kHz tone at 8 MSPS should still peak at 100 kHz after
    resampling down to 2.56 MSPS."""
    fs_src, fs_dst = 8_000_000, 2_560_000
    duration_s = 0.05
    f0 = 100_000

    t = np.arange(int(fs_src * duration_s)) / fs_src
    src = np.exp(2j * np.pi * f0 * t).astype(np.complex64)

    dst = resample_iq(src, fs_src, fs_dst)

    expected_len = int(round(len(src) * fs_dst / fs_src))
    assert abs(len(dst) - expected_len) < 100, f"length off: {len(dst)} vs {expected_len}"

    freqs = np.fft.fftshift(np.fft.fftfreq(len(dst), 1 / fs_dst))
    spectrum = np.abs(np.fft.fftshift(np.fft.fft(dst)))
    peak_freq = freqs[int(np.argmax(spectrum))]
    assert abs(peak_freq - f0) < 100, f"tone moved to {peak_freq:.0f} Hz"


def test_resample_iq_identity_short_circuits():
    """src_rate == dst_rate returns the same array (object identity)."""
    x = np.zeros(1000, dtype=np.complex64)
    assert resample_iq(x, 2_560_000, 2_560_000) is x


def test_drift_correction_compresses_fast_stream():
    """A clock that ran 100 ppm fast produced extra samples; correction
    should bring the count back to nominal, not stretch it further."""
    fs_nominal = 2_560_000
    duration_s = 10.0
    n_actual = int(fs_nominal * (1 + 100e-6) * duration_s)
    n_nominal = int(fs_nominal * duration_s)

    rng = np.random.default_rng(0)
    iq = (rng.standard_normal(n_actual)
          + 1j * rng.standard_normal(n_actual)).astype(np.complex64)

    corrected = apply_drift_correction(iq, drift_ppm=100.0, sample_rate=fs_nominal)
    assert abs(len(corrected) - n_nominal) <= 1, (
        f"correction yielded {len(corrected)} samples, expected ~{n_nominal}"
    )


def test_drift_correction_negligible_ppm_is_noop():
    """Below 0.1 ppm the function returns the input unchanged."""
    x = np.zeros(1000, dtype=np.complex64)
    assert apply_drift_correction(x, drift_ppm=0.05, sample_rate=2_560_000) is x


def test_estimate_offset_correlation_finds_known_shift():
    """Synthetic shifted IQ should be detected with the documented sign
    convention (positive offset = sig2 leads).

    Stays under the function's 100k-sample subsampling threshold so the
    test isn't sensitive to the odd-shift / subsampling interaction
    (real captures aren't iid noise, so subsampling works for them; pure
    noise here would fail at large N with odd shifts).
    """
    rng = np.random.default_rng(0)
    shift = 137
    n = 50_000
    base = (rng.standard_normal(n + shift)
            + 1j * rng.standard_normal(n + shift)).astype(np.complex64)

    # sig2 leads by `shift` → expect positive offset
    sig1 = base[:n]
    sig2 = base[shift:shift + n]
    offset, _peak, conf = estimate_offset_correlation(sig1, sig2)
    assert abs(offset - shift) <= 1, f"got {offset}, expected ~{shift}"
    assert conf > 0.1, f"confidence too low: {conf}"

    # Also verify the sign convention for the reverse case
    sig1 = base[shift:shift + n]
    sig2 = base[:n]
    offset_rev, _, _ = estimate_offset_correlation(sig1, sig2)
    assert abs(offset_rev + shift) <= 1, f"got {offset_rev}, expected ~{-shift}"


# ---------------------------------------------------------------------------
# Loader tests — exercise the byte-level normalization the data depends on
# ---------------------------------------------------------------------------

def test_load_rtlsdr_iq_normalization(tmp_path):
    """uint8 0/127/128/255 should normalize to roughly -1/0/0/+1."""
    # Bytes [I0, Q0, I1, Q1, I2, Q2, I3, Q3]
    raw = np.array([0, 0, 127, 127, 128, 128, 255, 255], dtype=np.uint8)
    f = tmp_path / "rtl.bin"
    f.write_bytes(raw.tobytes())

    iq = load_rtlsdr_iq(f)
    assert iq.dtype == np.complex64
    assert len(iq) == 4
    assert iq[0].real == pytest.approx(-1.0, abs=0.01)  # 0 → very negative
    assert iq[1].real == pytest.approx(0.0, abs=0.01)   # 127 → ~0
    assert iq[3].real == pytest.approx(1.0, abs=0.01)   # 255 → very positive


def test_load_hackrf_iq_normalization(tmp_path):
    """int8 -128/-1/0/127 should normalize symmetrically around 0."""
    raw = np.array([-128, -128, -1, -1, 0, 0, 127, 127], dtype=np.int8)
    f = tmp_path / "hackrf.bin"
    f.write_bytes(raw.tobytes())

    iq = load_hackrf_iq(f)
    assert iq.dtype == np.complex64
    assert len(iq) == 4
    assert iq[0].real == pytest.approx(-1.0, abs=0.01)
    assert iq[2].real == pytest.approx(0.0, abs=0.01)
    assert iq[3].real == pytest.approx(127 / 128, abs=0.01)


# ---------------------------------------------------------------------------
# Integration: real session through compute_range_doppler
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_SESSION = REPO_ROOT / "data" / "20251123_202750"


def _has_test_session() -> bool:
    return all(
        (TEST_SESSION / name).exists()
        for name in ("rtlsdr_left.bin", "rtlsdr_right.bin", "hackrf.bin", "metadata.json")
    )


@pytest.fixture(scope="module")
def processor():
    if not _has_test_session():
        pytest.skip(f"test session not present at {TEST_SESSION}")
    return WiFiCameraProcessor(TEST_SESSION)


def _assert_finite_with_signal(rdm, label):
    assert np.all(np.isfinite(rdm)), f"{label}: range-Doppler map contains NaN/inf"
    # Peak should rise meaningfully above the median (a flat noise floor
    # would give peak ≈ median; real targets give >20 dB lift).
    peak = float(np.max(rdm))
    floor = float(np.median(rdm))
    assert peak - floor > 5.0, (
        f"{label}: peak {peak:.1f} dB only {peak - floor:.1f} dB above floor"
    )


def test_compute_range_doppler_rtlsdr_reference(processor):
    """Use the other RTL-SDR as reference — the path that always worked."""
    rdm, ranges, dopplers = processor.compute_range_doppler(
        use_hackrf_reference=False, surveillance_channel="left"
    )
    assert rdm.ndim == 2
    assert len(ranges) == rdm.shape[1]
    assert len(dopplers) == rdm.shape[0]
    _assert_finite_with_signal(rdm, "RTL-SDR reference")


def test_compute_range_doppler_hackrf_reference(processor):
    """Use the HackRF as reference (resampled to RTL-SDR rate). This path
    was a TODO no-op before the resampler landed."""
    rdm, ranges, dopplers = processor.compute_range_doppler(
        use_hackrf_reference=True, surveillance_channel="left"
    )
    assert rdm.ndim == 2
    assert len(ranges) == rdm.shape[1]
    assert len(dopplers) == rdm.shape[0]
    _assert_finite_with_signal(rdm, "HackRF reference (resampled)")
