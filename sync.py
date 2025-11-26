#!/usr/bin/env python3
"""
WiFi Camera - Synchronization Module

Provides tools for:
1. Post-capture alignment via cross-correlation
2. Clock drift measurement and compensation
3. Sample loss detection and validation
4. Multi-stream time alignment

Based on investigation of captured data:
- RTL-SDRs show ~10 ppm relative clock drift
- first_data_time reflects USB buffer delivery, not ADC start
- Cross-correlation is more accurate than timestamp-based alignment
- HackRF shows significant sample loss at 20 MSPS (USB bandwidth issue)
"""

import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Union
import json


@dataclass
class StreamInfo:
    """Information about a captured stream"""
    name: str
    file_path: Path
    sample_rate: float
    is_signed: bool  # True for HackRF (int8), False for RTL-SDR (uint8)
    first_data_time: float
    samples_written: int
    bytes_written: int
    
    @property
    def duration_seconds(self) -> float:
        """Actual duration based on samples written"""
        return self.samples_written / self.sample_rate
    
    @property
    def expected_samples(self) -> int:
        """Expected samples if no loss occurred (requires capture_duration)"""
        return self.samples_written  # Placeholder, set externally


@dataclass
class AlignmentResult:
    """Result of aligning two streams"""
    stream1_name: str
    stream2_name: str
    offset_samples: int  # Positive = stream2 leads stream1
    offset_seconds: float
    correlation_peak: float
    confidence: float  # 0-1, based on peak sharpness
    method: str  # 'cross_correlation', 'timestamp', etc.


@dataclass  
class DriftResult:
    """Result of clock drift measurement"""
    stream1_name: str
    stream2_name: str
    drift_ppm: float  # Relative drift in parts per million
    drift_samples_per_second: float
    offset_start_samples: int
    offset_end_samples: int
    measurement_duration_seconds: float
    confidence: float


@dataclass
class SyncReport:
    """Complete synchronization analysis report"""
    session_id: str
    capture_duration: float
    streams: Dict[str, StreamInfo]
    alignments: List[AlignmentResult]
    drift_measurements: List[DriftResult]
    sample_loss: Dict[str, float]  # stream_name -> loss percentage
    warnings: List[str]
    recommendations: List[str]


def load_iq_chunk(filepath: Path, offset_samples: int, num_samples: int,
                  signed: bool = False) -> np.ndarray:
    """
    Load a chunk of IQ data from file.
    
    Args:
        filepath: Path to binary IQ file
        offset_samples: Sample offset to start reading from
        num_samples: Number of samples to read
        signed: True for HackRF (int8), False for RTL-SDR (uint8)
    
    Returns:
        Complex64 array normalized to [-1, 1]
    """
    byte_offset = offset_samples * 2  # 2 bytes per sample (I + Q)
    byte_count = num_samples * 2
    
    if signed:
        raw = np.fromfile(filepath, dtype=np.int8, offset=byte_offset, count=byte_count)
        raw = raw.reshape(-1, 2)
        iq = raw[:, 0].astype(np.float32) / 128.0 + \
             1j * raw[:, 1].astype(np.float32) / 128.0
    else:
        raw = np.fromfile(filepath, dtype=np.uint8, offset=byte_offset, count=byte_count)
        raw = raw.reshape(-1, 2)
        iq = (raw[:, 0].astype(np.float32) - 127.5) / 127.5 + \
             1j * (raw[:, 1].astype(np.float32) - 127.5) / 127.5
    
    return iq


def estimate_offset_correlation(sig1: np.ndarray, sig2: np.ndarray,
                                max_lag_samples: int = 50000) -> Tuple[int, float, float]:
    """
    Estimate time offset between two signals using cross-correlation.
    
    Uses envelope (magnitude) correlation which is more robust to phase differences.
    
    Args:
        sig1, sig2: Complex IQ signals
        max_lag_samples: Maximum lag to search in each direction
    
    Returns:
        Tuple of (offset_samples, correlation_peak, confidence)
        
        Offset interpretation:
        - Negative offset: sig1 leads (sig1 data arrives earlier)
          To align: trim |offset| samples from sig1 start
        - Positive offset: sig2 leads (sig2 data arrives earlier)  
          To align: trim offset samples from sig2 start
    """
    # Limit input size for efficiency and numerical stability
    max_samples = min(500000, len(sig1), len(sig2))
    s1 = sig1[:max_samples]
    s2 = sig2[:max_samples]
    
    # Use envelope correlation (more robust to phase)
    env1 = np.abs(s1)
    env2 = np.abs(s2)
    
    # Remove DC component
    env1 = env1 - np.mean(env1)
    env2 = env2 - np.mean(env2)
    
    # Use numpy's correlate with 'full' mode for simplicity
    # This is O(n²) but more reliable than FFT for our use case
    # For large arrays, we subsample
    if max_samples > 100000:
        step = max_samples // 100000
        env1 = env1[::step]
        env2 = env2[::step]
        effective_max_lag = max_lag_samples // step
    else:
        effective_max_lag = max_lag_samples
    
    corr = np.correlate(env1, env2, mode='full')
    
    # Center index corresponds to zero lag
    center = len(corr) // 2
    
    # Search within max_lag range
    search_start = max(0, center - effective_max_lag)
    search_end = min(len(corr), center + effective_max_lag)
    search_region = corr[search_start:search_end]
    
    # Find peak
    peak_idx_in_search = np.argmax(search_region)
    peak_val = search_region[peak_idx_in_search]
    
    # Convert to offset (positive = sig2 leads)
    offset = peak_idx_in_search - (center - search_start)
    
    # Scale back if we subsampled
    if max_samples > 100000:
        offset = offset * step
    
    # Normalize peak value
    peak_normalized = peak_val / (len(env1) * np.std(env1) * np.std(env2) + 1e-10)
    
    # Confidence based on peak sharpness
    # Compare peak to nearby values
    margin = min(100, len(search_region) // 10)
    if peak_idx_in_search > margin and peak_idx_in_search < len(search_region) - margin:
        nearby = np.concatenate([
            search_region[peak_idx_in_search - margin:peak_idx_in_search - 10],
            search_region[peak_idx_in_search + 10:peak_idx_in_search + margin]
        ])
        nearby_mean = np.mean(nearby)
        confidence = min(1.0, (peak_val - nearby_mean) / (peak_val + 1e-10))
    else:
        confidence = 0.5
    
    return int(offset), float(peak_normalized), float(max(0, confidence))


def measure_clock_drift(filepath1: Path, filepath2: Path,
                        sample_rate: float,
                        signed1: bool = False, signed2: bool = False,
                        chunk_samples: int = 2400000,
                        num_chunks: int = 10) -> DriftResult:
    """
    Measure clock drift between two streams by comparing alignment at different times.
    
    Loads chunks from beginning, middle, and end of capture to measure drift.
    
    Args:
        filepath1, filepath2: Paths to IQ files
        sample_rate: Sample rate in Hz
        signed1, signed2: Whether files are signed (HackRF) or unsigned (RTL-SDR)
        chunk_samples: Samples per chunk for correlation
        num_chunks: Number of chunks to analyze
    
    Returns:
        DriftResult with drift measurements
    """
    # Get file sizes
    size1 = filepath1.stat().st_size // 2
    size2 = filepath2.stat().st_size // 2
    min_samples = min(size1, size2)
    
    # Calculate chunk positions spread across the file
    usable_samples = min_samples - chunk_samples
    if usable_samples < chunk_samples:
        raise ValueError("Files too short for drift measurement")
    
    positions = np.linspace(0, usable_samples - chunk_samples, num_chunks, dtype=int)
    
    offsets = []
    for pos in positions:
        chunk1 = load_iq_chunk(filepath1, pos, chunk_samples, signed1)
        chunk2 = load_iq_chunk(filepath2, pos, chunk_samples, signed2)
        
        offset, _, _ = estimate_offset_correlation(chunk1, chunk2, max_lag_samples=20000)
        offsets.append(offset)
    
    offsets = np.array(offsets)
    times = positions / sample_rate
    
    # Linear regression to find drift rate
    # offset = offset_0 + drift_rate * time
    A = np.vstack([times, np.ones(len(times))]).T
    drift_rate, offset_0 = np.linalg.lstsq(A, offsets, rcond=None)[0]
    
    # Calculate ppm
    drift_ppm = drift_rate / sample_rate * 1e6
    
    # Confidence based on fit quality
    predicted = offset_0 + drift_rate * times
    residuals = offsets - predicted
    r_squared = 1 - np.sum(residuals**2) / np.sum((offsets - np.mean(offsets))**2)
    confidence = max(0, r_squared)
    
    duration = times[-1] - times[0]
    
    return DriftResult(
        stream1_name=filepath1.stem,
        stream2_name=filepath2.stem,
        drift_ppm=float(drift_ppm),
        drift_samples_per_second=float(drift_rate),
        offset_start_samples=int(offsets[0]),
        offset_end_samples=int(offsets[-1]),
        measurement_duration_seconds=float(duration),
        confidence=float(confidence)
    )


def detect_sample_loss(stream_info: StreamInfo, capture_duration: float) -> float:
    """
    Detect sample loss by comparing actual vs expected sample count.
    
    Args:
        stream_info: Stream information
        capture_duration: Total capture duration in seconds
    
    Returns:
        Sample loss as percentage (positive = loss, negative = extra samples)
    """
    stream_duration = capture_duration - (stream_info.first_data_time - capture_duration)
    # Actually, we need the capture start time
    expected_samples = stream_info.duration_seconds * stream_info.sample_rate
    actual_samples = stream_info.samples_written
    
    loss_pct = (1 - actual_samples / expected_samples) * 100 if expected_samples > 0 else 0
    return loss_pct


def apply_drift_correction(iq_data: np.ndarray, drift_ppm: float,
                           sample_rate: float) -> np.ndarray:
    """
    Apply clock drift correction via resampling.
    
    Args:
        iq_data: Complex IQ data to correct
        drift_ppm: Drift in parts per million (positive = this stream is fast)
        sample_rate: Original sample rate
    
    Returns:
        Resampled IQ data with drift corrected
    """
    if abs(drift_ppm) < 0.1:
        return iq_data  # Negligible drift
    
    # Calculate resampling ratio
    # If drift is positive (this stream is fast), we need to stretch it (upsample)
    ratio = 1 + drift_ppm * 1e-6
    
    new_length = int(len(iq_data) * ratio)
    
    # Use linear interpolation for speed (could use scipy.signal.resample for quality)
    old_indices = np.arange(len(iq_data))
    new_indices = np.linspace(0, len(iq_data) - 1, new_length)
    
    # Interpolate real and imaginary parts separately
    resampled = np.interp(new_indices, old_indices, iq_data.real) + \
                1j * np.interp(new_indices, old_indices, iq_data.imag)
    
    return resampled.astype(np.complex64)


def align_streams(stream1: np.ndarray, stream2: np.ndarray,
                  offset_samples: int) -> Tuple[np.ndarray, np.ndarray]:
    """
    Align two streams based on calculated offset.
    
    Args:
        stream1, stream2: IQ data arrays
        offset_samples: Offset in samples (positive = stream2 leads)
    
    Returns:
        Tuple of aligned (stream1, stream2) with same length
    """
    if offset_samples > 0:
        # stream2 leads, trim its start
        stream2 = stream2[offset_samples:]
    elif offset_samples < 0:
        # stream1 leads, trim its start
        stream1 = stream1[-offset_samples:]
    
    # Truncate to same length
    min_len = min(len(stream1), len(stream2))
    return stream1[:min_len], stream2[:min_len]


class SessionSync:
    """Synchronization analysis for a capture session"""
    
    def __init__(self, session_dir: Union[str, Path]):
        self.session_dir = Path(session_dir)
        self.timing: Dict = {}
        self.metadata: Dict = {}
        self.streams: Dict[str, StreamInfo] = {}
        
        self._load_session_data()
    
    def _load_session_data(self):
        """Load timing and metadata from session"""
        timing_file = self.session_dir / "timing.json"
        metadata_file = self.session_dir / "metadata.json"
        
        if timing_file.exists():
            with open(timing_file) as f:
                self.timing = json.load(f)
        
        if metadata_file.exists():
            with open(metadata_file) as f:
                self.metadata = json.load(f)
        
        # Build stream info
        sample_rates = self.timing.get('sample_rates', {})
        
        for name, data in self.timing.get('streams', {}).items():
            if name == 'webcam':
                continue  # Skip webcam for now
            
            filepath = self.session_dir / f"{name}.bin"
            if not filepath.exists():
                continue
            
            is_signed = 'hackrf' in name
            rate = sample_rates.get('hackrf' if is_signed else 'rtlsdr', 2400000)
            
            self.streams[name] = StreamInfo(
                name=name,
                file_path=filepath,
                sample_rate=rate,
                is_signed=is_signed,
                first_data_time=data.get('first_data_time', 0),
                samples_written=data.get('samples_written', 0),
                bytes_written=data.get('bytes_written', 0),
            )
    
    def analyze_alignment(self, chunk_samples: int = 500000) -> List[AlignmentResult]:
        """
        Analyze alignment between all stream pairs.
        """
        results = []
        stream_names = list(self.streams.keys())
        
        for i, name1 in enumerate(stream_names):
            for name2 in stream_names[i+1:]:
                s1 = self.streams[name1]
                s2 = self.streams[name2]
                
                # Skip if different sample rates (would need resampling)
                if s1.sample_rate != s2.sample_rate:
                    continue
                
                # Load chunks
                iq1 = load_iq_chunk(s1.file_path, 0, chunk_samples, s1.is_signed)
                iq2 = load_iq_chunk(s2.file_path, 0, chunk_samples, s2.is_signed)
                
                offset, peak, confidence = estimate_offset_correlation(iq1, iq2)
                
                results.append(AlignmentResult(
                    stream1_name=name1,
                    stream2_name=name2,
                    offset_samples=offset,
                    offset_seconds=offset / s1.sample_rate,
                    correlation_peak=peak,
                    confidence=confidence,
                    method='cross_correlation'
                ))
        
        return results
    
    def analyze_drift(self) -> List[DriftResult]:
        """
        Analyze clock drift between stream pairs.
        """
        results = []
        stream_names = list(self.streams.keys())
        
        for i, name1 in enumerate(stream_names):
            for name2 in stream_names[i+1:]:
                s1 = self.streams[name1]
                s2 = self.streams[name2]
                
                # Skip if different sample rates
                if s1.sample_rate != s2.sample_rate:
                    continue
                
                try:
                    drift = measure_clock_drift(
                        s1.file_path, s2.file_path,
                        s1.sample_rate,
                        s1.is_signed, s2.is_signed
                    )
                    results.append(drift)
                except Exception as e:
                    print(f"Warning: Could not measure drift between {name1} and {name2}: {e}")
        
        return results
    
    def analyze_sample_loss(self) -> Dict[str, float]:
        """
        Analyze sample loss for each stream.
        """
        capture_duration = self.timing.get('capture_stop_time', 0) - \
                          self.timing.get('capture_start_time', 0)
        
        loss = {}
        for name, stream in self.streams.items():
            stream_duration = self.timing.get('capture_stop_time', 0) - stream.first_data_time
            expected = stream_duration * stream.sample_rate
            actual = stream.samples_written
            loss[name] = (1 - actual / expected) * 100 if expected > 0 else 0
        
        return loss
    
    def generate_report(self) -> SyncReport:
        """
        Generate complete synchronization report.
        """
        alignments = self.analyze_alignment()
        drifts = self.analyze_drift()
        sample_loss = self.analyze_sample_loss()
        
        warnings = []
        recommendations = []
        
        # Check for issues
        for name, loss in sample_loss.items():
            if loss > 5:
                warnings.append(f"{name}: {loss:.1f}% sample loss detected")
                if 'hackrf' in name:
                    recommendations.append(
                        "Consider reducing HackRF sample rate to 10 MSPS to avoid USB bandwidth issues"
                    )
        
        for drift in drifts:
            if abs(drift.drift_ppm) > 20:
                warnings.append(
                    f"High clock drift ({drift.drift_ppm:.1f} ppm) between "
                    f"{drift.stream1_name} and {drift.stream2_name}"
                )
                recommendations.append(
                    "Consider using PPM correction in RTL-SDR config to compensate"
                )
        
        for align in alignments:
            if abs(align.offset_seconds) > 0.1:
                warnings.append(
                    f"Large alignment offset ({align.offset_seconds*1000:.1f} ms) between "
                    f"{align.stream1_name} and {align.stream2_name}"
                )
        
        return SyncReport(
            session_id=self.session_dir.name,
            capture_duration=self.timing.get('capture_stop_time', 0) - \
                            self.timing.get('capture_start_time', 0),
            streams=self.streams,
            alignments=alignments,
            drift_measurements=drifts,
            sample_loss=sample_loss,
            warnings=warnings,
            recommendations=recommendations
        )


def print_sync_report(report: SyncReport):
    """Pretty print a synchronization report"""
    print("=" * 70)
    print(f"Synchronization Report: {report.session_id}")
    print("=" * 70)
    print(f"Capture duration: {report.capture_duration:.1f}s")
    print()
    
    print("Streams:")
    print("-" * 70)
    for name, stream in report.streams.items():
        loss = report.sample_loss.get(name, 0)
        status = "✓" if abs(loss) < 1 else "⚠" if abs(loss) < 5 else "✗"
        print(f"  {status} {name:15} {stream.samples_written:>12,} samples "
              f"({stream.duration_seconds:.1f}s) loss: {loss:+.2f}%")
    print()
    
    if report.alignments:
        print("Alignment (cross-correlation):")
        print("-" * 70)
        for a in report.alignments:
            print(f"  {a.stream1_name} vs {a.stream2_name}:")
            print(f"    Offset: {a.offset_samples:+d} samples ({a.offset_seconds*1000:+.2f} ms)")
            print(f"    Confidence: {a.confidence:.2f}")
        print()
    
    if report.drift_measurements:
        print("Clock Drift:")
        print("-" * 70)
        for d in report.drift_measurements:
            print(f"  {d.stream1_name} vs {d.stream2_name}:")
            print(f"    Drift: {d.drift_ppm:+.2f} ppm ({d.drift_samples_per_second:+.1f} samples/s)")
            print(f"    Over {d.measurement_duration_seconds:.0f}s, confidence: {d.confidence:.2f}")
        print()
    
    if report.warnings:
        print("⚠ Warnings:")
        print("-" * 70)
        for w in report.warnings:
            print(f"  • {w}")
        print()
    
    if report.recommendations:
        print("Recommendations:")
        print("-" * 70)
        for r in report.recommendations:
            print(f"  → {r}")
        print()


def main():
    """Analyze synchronization for a session"""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python sync.py <session_dir>")
        print("Example: python sync.py data/20251123_225002")
        sys.exit(1)
    
    session_dir = Path(sys.argv[1])
    if not session_dir.exists():
        print(f"Error: Session directory not found: {session_dir}")
        sys.exit(1)
    
    print(f"Analyzing synchronization for {session_dir.name}...")
    print()
    
    sync = SessionSync(session_dir)
    report = sync.generate_report()
    print_sync_report(report)


if __name__ == "__main__":
    main()

