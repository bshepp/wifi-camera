#!/usr/bin/env python3
"""
Align Data - Load synchronized IQ captures with corresponding webcam frames

Creates aligned data packages containing:
- IQ data from all devices that captured within the time window
- Corresponding webcam frame
- Timing metadata for each capture

Can export as:
- NumPy arrays (.npz)
- Individual aligned packages
- Full session alignment
"""

import json
import sys
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import argparse
from PIL import Image


@dataclass
class AlignedCapture:
    """Single device capture in an aligned group"""
    device: str
    band: str
    frequency: int
    filename: str
    timestamp: float
    iq_data: Optional[np.ndarray] = None


@dataclass
class AlignedPackage:
    """Package of time-aligned data from multiple sources"""
    group_index: int
    timestamp: float
    elapsed_s: float
    frame_index: int
    frame_file: str
    time_spread_ms: float
    captures: Dict[str, AlignedCapture]
    frame_data: Optional[np.ndarray] = None
    
    def has_device(self, device: str) -> bool:
        return device in self.captures
    
    @property
    def devices(self) -> List[str]:
        return list(self.captures.keys())
    
    def get_iq(self, device: str) -> Optional[np.ndarray]:
        if device in self.captures:
            return self.captures[device].iq_data
        return None


class DataAligner:
    """Aligns IQ data with webcam frames"""
    
    def __init__(self, session_dir: Path):
        self.session_dir = session_dir
        self.correlation = None
        self.metadata = None
        self.aligned_packages: List[AlignedPackage] = []
        
        # Sample rates for IQ loading
        self.sample_rates = {
            'left': 2_560_000,
            'right': 2_560_000,
            'hackrf': 8_000_000
        }
        
        # IQ data format
        self.iq_dtype = {
            'left': np.uint8,    # RTL-SDR: unsigned
            'right': np.uint8,
            'hackrf': np.int8   # HackRF: signed
        }
        
    def load_correlation(self) -> bool:
        """Load correlation index"""
        corr_file = self.session_dir / "correlation_index.json"
        if not corr_file.exists():
            print(f"ERROR: No correlation_index.json found. Run correlate_captures.py first.")
            return False
            
        with open(corr_file) as f:
            self.correlation = json.load(f)
            
        meta_file = self.session_dir / "metadata.json"
        if meta_file.exists():
            with open(meta_file) as f:
                self.metadata = json.load(f)
                
        return True
    
    def load_iq_file(self, device: str, filename: str) -> Optional[np.ndarray]:
        """Load IQ data from binary file and convert to complex"""
        
        # Determine path based on device
        if device == 'hackrf':
            filepath = self.session_dir / "iq_data" / "hackrf" / filename
        else:
            filepath = self.session_dir / "iq_data" / device / filename
            
        if not filepath.exists():
            print(f"  Warning: IQ file not found: {filepath}")
            return None
            
        # Load raw bytes
        dtype = self.iq_dtype.get(device, np.uint8)
        raw = np.fromfile(filepath, dtype=dtype)
        
        # Convert to complex
        # IQ format: I0, Q0, I1, Q1, ...
        if len(raw) % 2 != 0:
            raw = raw[:-1]  # Trim odd byte
            
        i_samples = raw[0::2].astype(np.float32)
        q_samples = raw[1::2].astype(np.float32)
        
        # Normalize based on data type
        if dtype == np.uint8:
            # RTL-SDR: unsigned 8-bit, center at 127.5
            i_samples = (i_samples - 127.5) / 127.5
            q_samples = (q_samples - 127.5) / 127.5
        else:
            # HackRF: signed 8-bit, center at 0
            i_samples = i_samples / 128.0
            q_samples = q_samples / 128.0
            
        # Create complex array
        iq = i_samples + 1j * q_samples
        
        return iq
    
    def load_frame(self, frame_file: str) -> Optional[np.ndarray]:
        """Load webcam frame as numpy array"""
        filepath = self.session_dir / "frames" / frame_file
        
        if not filepath.exists():
            print(f"  Warning: Frame not found: {filepath}")
            return None
            
        try:
            img = Image.open(filepath)
            return np.array(img)
        except Exception as e:
            print(f"  Warning: Failed to load frame: {e}")
            return None
    
    def build_aligned_packages(self, load_iq: bool = True, load_frames: bool = True) -> int:
        """Build aligned data packages from correlation index"""
        
        if not self.correlation:
            return 0
            
        aligned_groups = self.correlation.get('aligned_groups', [])
        
        print(f"Building {len(aligned_groups)} aligned packages...")
        
        for i, group in enumerate(aligned_groups):
            captures = {}
            
            for device, cap_info in group.get('captures', {}).items():
                capture = AlignedCapture(
                    device=device,
                    band=cap_info['band'],
                    frequency=cap_info['frequency'],
                    filename=cap_info['filename'],
                    timestamp=group['timestamp']
                )
                
                if load_iq:
                    capture.iq_data = self.load_iq_file(device, cap_info['filename'])
                    
                captures[device] = capture
            
            # Determine frame file
            frame_idx = group['frame_index']
            frame_file = f"frame_{frame_idx+1:06d}.jpg"  # Frames are 1-indexed in filenames
            
            package = AlignedPackage(
                group_index=i,
                timestamp=group['timestamp'],
                elapsed_s=group['elapsed_s'],
                frame_index=frame_idx,
                frame_file=frame_file,
                time_spread_ms=group['time_spread_ms'],
                captures=captures
            )
            
            if load_frames:
                package.frame_data = self.load_frame(frame_file)
                
            self.aligned_packages.append(package)
            
            # Progress
            if (i + 1) % 10 == 0 or i == len(aligned_groups) - 1:
                print(f"  Loaded {i + 1}/{len(aligned_groups)} packages")
                
        return len(self.aligned_packages)
    
    def export_npz(self, output_file: Path, max_packages: int = None):
        """Export aligned data as NumPy archive"""
        
        packages_to_export = self.aligned_packages
        if max_packages:
            packages_to_export = packages_to_export[:max_packages]
            
        export_data = {
            'num_packages': len(packages_to_export),
            'timestamps': np.array([p.timestamp for p in packages_to_export]),
            'elapsed_s': np.array([p.elapsed_s for p in packages_to_export]),
            'frame_indices': np.array([p.frame_index for p in packages_to_export]),
            'time_spreads_ms': np.array([p.time_spread_ms for p in packages_to_export]),
        }
        
        # Add IQ data per device
        for device in ['left', 'right', 'hackrf']:
            iq_list = []
            has_device = []
            
            for pkg in packages_to_export:
                if pkg.has_device(device) and pkg.captures[device].iq_data is not None:
                    iq_list.append(pkg.captures[device].iq_data)
                    has_device.append(True)
                else:
                    iq_list.append(np.array([], dtype=np.complex64))
                    has_device.append(False)
                    
            export_data[f'{device}_has_data'] = np.array(has_device)
            export_data[f'{device}_iq'] = np.array(iq_list, dtype=object)
            
            # Band info
            bands = [pkg.captures[device].band if pkg.has_device(device) else '' 
                    for pkg in packages_to_export]
            export_data[f'{device}_bands'] = np.array(bands)
            
            freqs = [pkg.captures[device].frequency if pkg.has_device(device) else 0
                    for pkg in packages_to_export]
            export_data[f'{device}_frequencies'] = np.array(freqs)
        
        # Add frames if available
        frames = []
        for pkg in packages_to_export:
            if pkg.frame_data is not None:
                frames.append(pkg.frame_data)
            else:
                frames.append(np.array([]))
        export_data['frames'] = np.array(frames, dtype=object)
        
        np.savez_compressed(output_file, **export_data)
        
        return len(packages_to_export)
    
    def print_summary(self):
        """Print summary of aligned data"""
        print("\n" + "=" * 70)
        print("ALIGNED DATA SUMMARY")
        print("=" * 70)
        
        print(f"Total aligned packages: {len(self.aligned_packages)}")
        
        # Device coverage
        device_counts = {'left': 0, 'right': 0, 'hackrf': 0}
        for pkg in self.aligned_packages:
            for device in pkg.devices:
                device_counts[device] = device_counts.get(device, 0) + 1
                
        print(f"\nDevice coverage:")
        for device, count in device_counts.items():
            pct = 100 * count / len(self.aligned_packages) if self.aligned_packages else 0
            print(f"  {device}: {count} packages ({pct:.0f}%)")
        
        # Band distribution
        print(f"\nBand distribution:")
        band_counts = {}
        for pkg in self.aligned_packages:
            for device, cap in pkg.captures.items():
                key = f"{device}:{cap.band}"
                band_counts[key] = band_counts.get(key, 0) + 1
                
        for key, count in sorted(band_counts.items()):
            print(f"  {key}: {count}")
            
        # IQ data stats
        print(f"\nIQ data loaded:")
        for device in ['left', 'right', 'hackrf']:
            loaded = sum(1 for pkg in self.aligned_packages 
                        if pkg.has_device(device) and pkg.captures[device].iq_data is not None)
            total = sum(1 for pkg in self.aligned_packages if pkg.has_device(device))
            if total > 0:
                sample_pkg = next((pkg for pkg in self.aligned_packages 
                                  if pkg.has_device(device) and pkg.captures[device].iq_data is not None), None)
                if sample_pkg:
                    iq = sample_pkg.captures[device].iq_data
                    print(f"  {device}: {loaded}/{total} files, {len(iq):,} samples each")
                    
        # Frame stats
        frames_loaded = sum(1 for pkg in self.aligned_packages if pkg.frame_data is not None)
        print(f"\nFrames loaded: {frames_loaded}/{len(self.aligned_packages)}")
        
        if frames_loaded > 0:
            sample_frame = next(pkg.frame_data for pkg in self.aligned_packages if pkg.frame_data is not None)
            print(f"  Frame shape: {sample_frame.shape}")
            
        print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description='Align IQ data with webcam frames')
    parser.add_argument('session_dir', type=str, help='Path to capture session directory')
    parser.add_argument('--export', '-e', type=str, help='Export to NPZ file')
    parser.add_argument('--max', '-m', type=int, help='Maximum packages to process')
    parser.add_argument('--no-iq', action='store_true', help='Skip loading IQ data')
    parser.add_argument('--no-frames', action='store_true', help='Skip loading frames')
    
    args = parser.parse_args()
    
    session_dir = Path(args.session_dir)
    if not session_dir.exists():
        print(f"ERROR: Session directory not found: {session_dir}")
        sys.exit(1)
    
    aligner = DataAligner(session_dir)
    
    print(f"Loading session: {session_dir}")
    if not aligner.load_correlation():
        print("Run correlate_captures.py first to generate correlation_index.json")
        sys.exit(1)
    
    # Build aligned packages
    num_packages = aligner.build_aligned_packages(
        load_iq=not args.no_iq,
        load_frames=not args.no_frames
    )
    
    if num_packages == 0:
        print("No aligned packages found!")
        sys.exit(1)
    
    # Print summary
    aligner.print_summary()
    
    # Export if requested
    if args.export:
        output_file = Path(args.export)
        max_pkgs = args.max
        
        print(f"\nExporting to: {output_file}")
        num_exported = aligner.export_npz(output_file, max_packages=max_pkgs)
        print(f"Exported {num_exported} aligned packages")
        
        # Show file size
        if output_file.exists():
            size_mb = output_file.stat().st_size / 1024 / 1024
            print(f"File size: {size_mb:.1f} MB")


if __name__ == "__main__":
    main()

