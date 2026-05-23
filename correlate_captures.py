#!/usr/bin/env python3
"""
Correlate Captures - Time-align IQ data with webcam frames

Analyzes capture session data to:
1. Generate frame timestamps from webcam start time
2. Map each IQ capture to nearest webcam frame(s)
3. Find time-aligned capture groups across devices
4. Output correlation report and aligned data index
"""

import json
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
import argparse


@dataclass
class IQCapture:
    """Single IQ capture record"""
    device: str
    band: str
    frequency: int
    timestamp: float
    filename: str
    samples: int
    pass_num: int
    
    @property
    def duration_s(self) -> float:
        """Capture duration based on samples and assumed rate"""
        if 'hackrf' in self.device:
            return self.samples / 8_000_000 / 2  # 8 MSPS, /2 for IQ pairs
        else:
            return self.samples / 2_560_000 / 2  # 2.56 MSPS


@dataclass 
class Frame:
    """Webcam frame"""
    index: int
    timestamp: float
    filename: str


@dataclass
class AlignedGroup:
    """Group of captures aligned in time"""
    timestamp: float
    frame_index: int
    captures: Dict[str, IQCapture]  # device -> capture
    time_spread_ms: float


class CaptureCorrelator:
    """Correlates IQ captures with webcam frames"""
    
    def __init__(self, session_dir: Path):
        self.session_dir = session_dir
        self.metadata = None
        self.captures: Dict[str, List[IQCapture]] = {}
        self.frames: List[Frame] = []
        self.start_time = 0
        self.fps = 30.0
        
    def load(self) -> bool:
        """Load session data"""
        # Load metadata
        meta_file = self.session_dir / "metadata.json"
        if not meta_file.exists():
            print(f"ERROR: No metadata.json in {self.session_dir}")
            return False
            
        with open(meta_file) as f:
            self.metadata = json.load(f)
            
        self.start_time = self.metadata.get("start_time", 0)
        
        # Load capture logs
        for device in ["left", "right", "hackrf"]:
            cap_file = self.session_dir / f"captures_{device}.json"
            if cap_file.exists():
                with open(cap_file) as f:
                    data = json.load(f)
                    self.captures[device] = [
                        IQCapture(
                            device=device,
                            band=c["band"],
                            frequency=c["frequency"],
                            timestamp=c["timestamp"],
                            filename=c["filename"],
                            samples=c["samples"],
                            pass_num=c["pass"]
                        )
                        for c in data
                    ]
                    
        # Generate frame timestamps
        frames_dir = self.session_dir / "frames"
        if frames_dir.exists():
            frame_files = sorted(frames_dir.glob("frame_*.jpg"))
            for i, f in enumerate(frame_files):
                self.frames.append(Frame(
                    index=i,
                    timestamp=self.start_time + (i / self.fps),
                    filename=f.name
                ))
                
        return True
    
    def find_nearest_frame(self, timestamp: float) -> Tuple[int, float]:
        """Find frame nearest to timestamp, return (index, offset_ms)"""
        if not self.frames:
            return -1, 0
            
        # Calculate expected frame index
        elapsed = timestamp - self.start_time
        expected_idx = int(elapsed * self.fps)
        
        # Clamp to valid range
        idx = max(0, min(expected_idx, len(self.frames) - 1))
        
        # Calculate offset
        offset_ms = (timestamp - self.frames[idx].timestamp) * 1000
        
        return idx, offset_ms
    
    def find_aligned_groups(self, max_spread_ms: float = 100) -> List[AlignedGroup]:
        """Find groups of captures that occurred within max_spread_ms of each other"""
        
        # Collect all captures with timestamps
        all_captures = []
        for device, caps in self.captures.items():
            for cap in caps:
                all_captures.append(cap)
        
        # Sort by timestamp
        all_captures.sort(key=lambda c: c.timestamp)
        
        groups = []
        used = set()
        
        for i, anchor in enumerate(all_captures):
            if id(anchor) in used:
                continue
                
            # Start new group with this capture
            group_caps = {anchor.device: anchor}
            group_times = [anchor.timestamp]
            used.add(id(anchor))
            
            # Find other captures within time window
            for j, other in enumerate(all_captures):
                if id(other) in used:
                    continue
                if other.device in group_caps:
                    continue  # Already have this device
                    
                time_diff_ms = abs(other.timestamp - anchor.timestamp) * 1000
                if time_diff_ms <= max_spread_ms:
                    group_caps[other.device] = other
                    group_times.append(other.timestamp)
                    used.add(id(other))
            
            # Only keep groups with multiple devices
            if len(group_caps) >= 2:
                avg_time = sum(group_times) / len(group_times)
                spread = (max(group_times) - min(group_times)) * 1000
                frame_idx, _ = self.find_nearest_frame(avg_time)
                
                groups.append(AlignedGroup(
                    timestamp=avg_time,
                    frame_index=frame_idx,
                    captures=group_caps,
                    time_spread_ms=spread
                ))
        
        return groups
    
    def generate_report(self) -> str:
        """Generate correlation report"""
        lines = []
        lines.append("=" * 70)
        lines.append("CAPTURE CORRELATION REPORT")
        lines.append("=" * 70)
        lines.append(f"Session: {self.metadata.get('session_id', 'unknown')}")
        lines.append(f"Start Time: {self.start_time}")
        lines.append(f"Duration: {self.metadata.get('duration_seconds', 0)}s")
        lines.append("")
        
        # Device summary
        lines.append("DEVICES:")
        for device, caps in self.captures.items():
            if caps:
                bands = set(c.band for c in caps)
                lines.append(f"  {device}: {len(caps)} captures across {len(bands)} bands")
        lines.append(f"  webcam: {len(self.frames)} frames @ {self.fps} fps")
        lines.append("")
        
        # Time range
        all_timestamps = []
        for caps in self.captures.values():
            all_timestamps.extend(c.timestamp for c in caps)
        
        if all_timestamps:
            t_min = min(all_timestamps)
            t_max = max(all_timestamps)
            lines.append(f"CAPTURE TIME RANGE:")
            lines.append(f"  First capture: {t_min - self.start_time:.3f}s after start")
            lines.append(f"  Last capture:  {t_max - self.start_time:.3f}s after start")
            lines.append(f"  Span: {t_max - t_min:.1f}s")
            lines.append("")
        
        # Frame correlation for each device
        lines.append("FRAME CORRELATION:")
        for device, caps in self.captures.items():
            if not caps:
                continue
            offsets = []
            for cap in caps:
                _, offset_ms = self.find_nearest_frame(cap.timestamp)
                offsets.append(abs(offset_ms))
            
            avg_offset = sum(offsets) / len(offsets)
            max_offset = max(offsets)
            lines.append(f"  {device}: avg offset {avg_offset:.1f}ms, max {max_offset:.1f}ms")
        lines.append("")
        
        # Find aligned groups
        groups = self.find_aligned_groups(max_spread_ms=100)
        lines.append(f"ALIGNED CAPTURE GROUPS (within 100ms):")
        lines.append(f"  Found {len(groups)} groups with 2+ devices")
        
        if groups:
            # Stats on groups
            spreads = [g.time_spread_ms for g in groups]
            device_counts = {}
            for g in groups:
                for d in g.captures.keys():
                    device_counts[d] = device_counts.get(d, 0) + 1
            
            lines.append(f"  Avg time spread: {sum(spreads)/len(spreads):.1f}ms")
            lines.append(f"  Max time spread: {max(spreads):.1f}ms")
            lines.append(f"  Device participation:")
            for d, cnt in sorted(device_counts.items()):
                lines.append(f"    {d}: {cnt} groups ({100*cnt/len(groups):.0f}%)")
        lines.append("")
        
        # Sample aligned groups
        lines.append("SAMPLE ALIGNED GROUPS (first 10):")
        for i, group in enumerate(groups[:10]):
            elapsed = group.timestamp - self.start_time
            devices = ", ".join(f"{d}:{c.band}" for d, c in group.captures.items())
            lines.append(f"  [{i+1}] t={elapsed:.2f}s frame={group.frame_index} "
                        f"spread={group.time_spread_ms:.1f}ms | {devices}")
        
        lines.append("")
        lines.append("=" * 70)
        
        return "\n".join(lines)
    
    def export_correlation_index(self, output_file: Path):
        """Export correlation index as JSON"""
        
        groups = self.find_aligned_groups(max_spread_ms=100)
        
        # Build frame -> captures mapping
        frame_to_captures = {}
        for device, caps in self.captures.items():
            for cap in caps:
                frame_idx, offset_ms = self.find_nearest_frame(cap.timestamp)
                if frame_idx not in frame_to_captures:
                    frame_to_captures[frame_idx] = {
                        "frame_index": frame_idx,
                        "frame_file": self.frames[frame_idx].filename if frame_idx < len(self.frames) else None,
                        "frame_timestamp": self.frames[frame_idx].timestamp if frame_idx < len(self.frames) else None,
                        "captures": []
                    }
                frame_to_captures[frame_idx]["captures"].append({
                    "device": device,
                    "band": cap.band,
                    "frequency": cap.frequency,
                    "filename": cap.filename,
                    "timestamp": cap.timestamp,
                    "offset_ms": offset_ms
                })
        
        # Build aligned groups export. Embed the resolved frame filename so
        # downstream consumers (align_data, export_sagemaker) don't have to
        # reconstruct it — different capture scripts use different patterns
        # (ffmpeg's frame_000001.jpg vs capture.py's frame_000000_TS.jpg).
        aligned_groups = []
        for group in groups:
            frame_file = (
                self.frames[group.frame_index].filename
                if 0 <= group.frame_index < len(self.frames)
                else None
            )
            aligned_groups.append({
                "timestamp": group.timestamp,
                "elapsed_s": group.timestamp - self.start_time,
                "frame_index": group.frame_index,
                "frame_file": frame_file,
                "time_spread_ms": group.time_spread_ms,
                "captures": {
                    device: {
                        "band": cap.band,
                        "frequency": cap.frequency,
                        "filename": cap.filename
                    }
                    for device, cap in group.captures.items()
                }
            })
        
        export = {
            "session_id": self.metadata.get("session_id"),
            "start_time": self.start_time,
            "total_frames": len(self.frames),
            "total_captures": {d: len(c) for d, c in self.captures.items()},
            "aligned_groups_count": len(aligned_groups),
            "frame_to_captures": frame_to_captures,
            "aligned_groups": aligned_groups
        }
        
        with open(output_file, 'w') as f:
            json.dump(export, f, indent=2)
        
        return len(aligned_groups)


def main():
    parser = argparse.ArgumentParser(description='Correlate IQ captures with webcam frames')
    parser.add_argument('session_dir', type=str, help='Path to capture session directory')
    parser.add_argument('--export', '-e', action='store_true', help='Export correlation index JSON')
    parser.add_argument('--output', '-o', type=str, help='Output file for correlation index')
    
    args = parser.parse_args()
    
    session_dir = Path(args.session_dir)
    if not session_dir.exists():
        print(f"ERROR: Session directory not found: {session_dir}")
        sys.exit(1)
    
    correlator = CaptureCorrelator(session_dir)
    
    print(f"Loading session: {session_dir}")
    if not correlator.load():
        sys.exit(1)
    
    # Generate and print report
    report = correlator.generate_report()
    print(report)
    
    # Export if requested
    if args.export:
        output_file = Path(args.output) if args.output else session_dir / "correlation_index.json"
        num_groups = correlator.export_correlation_index(output_file)
        print(f"\nExported correlation index to: {output_file}")
        print(f"  {num_groups} aligned capture groups")
        print(f"  {len(correlator.frames)} frame mappings")


if __name__ == "__main__":
    main()

