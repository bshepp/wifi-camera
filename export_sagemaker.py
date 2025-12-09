#!/usr/bin/env python3
"""
Export to SageMaker-Compatible Format

Creates training data packages suitable for Amazon SageMaker:
- Individual sample directories with .npy arrays
- Manifest file (JSON Lines) for SageMaker input
- Optional: Sharded NPZ files for efficient loading
- Train/validation split

Data format per sample:
  sample_XXXX/
    ├── frame.npy       # (H, W, 3) uint8 - webcam frame
    ├── hackrf_iq.npy   # (N,) complex64 - HackRF IQ data
    ├── left_iq.npy     # (N,) complex64 - RTL-SDR left IQ
    ├── right_iq.npy    # (N,) complex64 - RTL-SDR right IQ  
    └── meta.json       # Timing, frequency, band info

Manifest format (JSON Lines):
  {"source": "s3://bucket/train/sample_0000/", "frame": "frame.npy", ...}
"""

import json
import sys
import numpy as np
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
import argparse
import random
from PIL import Image


@dataclass
class SampleMetadata:
    """Metadata for a single training sample"""
    sample_id: str
    timestamp: float
    elapsed_s: float
    frame_index: int
    time_spread_ms: float
    devices: List[str]
    bands: Dict[str, str]  # device -> band
    frequencies: Dict[str, int]  # device -> Hz


def load_iq_file(filepath: Path, device: str) -> Optional[np.ndarray]:
    """Load IQ data and convert to complex64"""
    if not filepath.exists():
        return None
        
    raw = np.fromfile(filepath, dtype=np.uint8 if 'rtl' in device else np.int8)
    
    # Convert to complex
    if 'hackrf' in device:
        # Signed int8
        i = raw[0::2].astype(np.float32) / 128.0
        q = raw[1::2].astype(np.float32) / 128.0
    else:
        # Unsigned int8 (RTL-SDR)
        i = (raw[0::2].astype(np.float32) - 127.5) / 127.5
        q = (raw[1::2].astype(np.float32) - 127.5) / 127.5
        
    return (i + 1j * q).astype(np.complex64)


def load_frame(filepath: Path) -> Optional[np.ndarray]:
    """Load webcam frame as numpy array"""
    if not filepath.exists():
        return None
    img = Image.open(filepath)
    return np.array(img, dtype=np.uint8)


class SageMakerExporter:
    """Export aligned data to SageMaker format"""
    
    def __init__(self, session_dir: Path, output_dir: Path):
        self.session_dir = session_dir
        self.output_dir = output_dir
        self.correlation = None
        self.metadata = None
        
    def load_session(self) -> bool:
        """Load correlation index and metadata"""
        corr_file = self.session_dir / "correlation_index.json"
        meta_file = self.session_dir / "metadata.json"
        
        if not corr_file.exists():
            print(f"ERROR: No correlation_index.json found")
            print(f"Run: ./correlate_captures.py {self.session_dir} --export")
            return False
            
        with open(corr_file) as f:
            self.correlation = json.load(f)
            
        with open(meta_file) as f:
            self.metadata = json.load(f)
            
        return True
    
    def export_samples(self, train_ratio: float = 0.8, 
                       max_samples: int = 0,
                       min_devices: int = 2) -> Dict:
        """Export individual samples with train/val split"""
        
        aligned_groups = self.correlation.get("aligned_groups", [])
        
        # Filter by minimum devices
        groups = [g for g in aligned_groups if len(g["captures"]) >= min_devices]
        
        if max_samples > 0:
            groups = groups[:max_samples]
            
        if not groups:
            print("No aligned groups to export!")
            return {}
        
        # Shuffle for random split
        random.shuffle(groups)
        
        split_idx = int(len(groups) * train_ratio)
        train_groups = groups[:split_idx]
        val_groups = groups[split_idx:]
        
        # Create output directories
        train_dir = self.output_dir / "train"
        val_dir = self.output_dir / "validation"
        train_dir.mkdir(parents=True, exist_ok=True)
        val_dir.mkdir(parents=True, exist_ok=True)
        
        stats = {
            "train_samples": 0,
            "val_samples": 0,
            "total_iq_bytes": 0,
            "total_frame_bytes": 0,
            "devices_coverage": {}
        }
        
        manifest_train = []
        manifest_val = []
        
        # Export training samples
        print(f"\nExporting {len(train_groups)} training samples...")
        for i, group in enumerate(train_groups):
            sample_id = f"sample_{i:04d}"
            sample_dir = train_dir / sample_id
            
            meta = self._export_single_sample(sample_dir, group, sample_id, stats)
            if meta:
                manifest_train.append({
                    "source": f"train/{sample_id}/",
                    **asdict(meta)
                })
                stats["train_samples"] += 1
                
            if (i + 1) % 10 == 0:
                print(f"  Exported {i + 1}/{len(train_groups)}")
        
        # Export validation samples
        print(f"\nExporting {len(val_groups)} validation samples...")
        for i, group in enumerate(val_groups):
            sample_id = f"sample_{i:04d}"
            sample_dir = val_dir / sample_id
            
            meta = self._export_single_sample(sample_dir, group, sample_id, stats)
            if meta:
                manifest_val.append({
                    "source": f"validation/{sample_id}/",
                    **asdict(meta)
                })
                stats["val_samples"] += 1
        
        # Write manifest files (JSON Lines format for SageMaker)
        with open(self.output_dir / "train_manifest.jsonl", 'w') as f:
            for entry in manifest_train:
                f.write(json.dumps(entry) + "\n")
                
        with open(self.output_dir / "validation_manifest.jsonl", 'w') as f:
            for entry in manifest_val:
                f.write(json.dumps(entry) + "\n")
        
        # Write combined metadata
        export_meta = {
            "source_session": self.metadata.get("session_id"),
            "train_samples": stats["train_samples"],
            "validation_samples": stats["val_samples"],
            "train_ratio": train_ratio,
            "min_devices": min_devices,
            "sample_format": {
                "frame": "frame.npy - uint8 (H, W, 3)",
                "hackrf_iq": "hackrf_iq.npy - complex64 (N,)",
                "left_iq": "left_iq.npy - complex64 (N,)",
                "right_iq": "right_iq.npy - complex64 (N,)",
                "meta": "meta.json - sample metadata"
            },
            "devices_coverage": stats["devices_coverage"]
        }
        
        with open(self.output_dir / "dataset_info.json", 'w') as f:
            json.dump(export_meta, f, indent=2)
            
        return stats
    
    def _export_single_sample(self, sample_dir: Path, group: Dict, 
                              sample_id: str, stats: Dict) -> Optional[SampleMetadata]:
        """Export a single aligned sample"""
        sample_dir.mkdir(parents=True, exist_ok=True)
        
        captures = group["captures"]
        frame_idx = group["frame_index"]
        
        # Build metadata
        meta = SampleMetadata(
            sample_id=sample_id,
            timestamp=group["timestamp"],
            elapsed_s=group["elapsed_s"],
            frame_index=frame_idx,
            time_spread_ms=group["time_spread_ms"],
            devices=list(captures.keys()),
            bands={d: c["band"] for d, c in captures.items()},
            frequencies={d: c["frequency"] for d, c in captures.items()}
        )
        
        # Track device coverage
        for device in meta.devices:
            stats["devices_coverage"][device] = stats["devices_coverage"].get(device, 0) + 1
        
        # Export IQ data for each device
        iq_dir = self.session_dir / "iq_data"
        
        for device, cap_info in captures.items():
            filename = cap_info["filename"]
            
            # Determine source directory
            if device == "hackrf":
                iq_file = iq_dir / "hackrf" / filename
                out_name = "hackrf_iq.npy"
            elif device == "left":
                iq_file = iq_dir / "left" / filename
                out_name = "left_iq.npy"
            elif device == "right":
                iq_file = iq_dir / "right" / filename
                out_name = "right_iq.npy"
            else:
                continue
            
            iq_data = load_iq_file(iq_file, device)
            if iq_data is not None:
                np.save(sample_dir / out_name, iq_data)
                stats["total_iq_bytes"] += iq_data.nbytes
        
        # Export frame
        frame_file = self.session_dir / "frames" / f"frame_{frame_idx + 1:06d}.jpg"
        frame_data = load_frame(frame_file)
        if frame_data is not None:
            np.save(sample_dir / "frame.npy", frame_data)
            stats["total_frame_bytes"] += frame_data.nbytes
        
        # Write sample metadata
        with open(sample_dir / "meta.json", 'w') as f:
            json.dump(asdict(meta), f, indent=2)
            
        return meta


def main():
    parser = argparse.ArgumentParser(
        description='Export aligned data to SageMaker-compatible format'
    )
    parser.add_argument('session_dir', type=str, 
                        help='Path to capture session directory')
    parser.add_argument('--output', '-o', type=str, required=True,
                        help='Output directory for SageMaker data')
    parser.add_argument('--train-ratio', type=float, default=0.8,
                        help='Training data ratio (default: 0.8)')
    parser.add_argument('--max-samples', type=int, default=0,
                        help='Maximum samples to export (0 = all)')
    parser.add_argument('--min-devices', type=int, default=2,
                        help='Minimum devices per sample (default: 2)')
    
    args = parser.parse_args()
    
    session_dir = Path(args.session_dir)
    output_dir = Path(args.output)
    
    if not session_dir.exists():
        print(f"ERROR: Session directory not found: {session_dir}")
        sys.exit(1)
    
    exporter = SageMakerExporter(session_dir, output_dir)
    
    print(f"Loading session: {session_dir}")
    if not exporter.load_session():
        sys.exit(1)
    
    print(f"Exporting to: {output_dir}")
    stats = exporter.export_samples(
        train_ratio=args.train_ratio,
        max_samples=args.max_samples,
        min_devices=args.min_devices
    )
    
    if not stats:
        sys.exit(1)
    
    # Summary
    print(f"\n{'='*60}")
    print("EXPORT COMPLETE")
    print(f"{'='*60}")
    print(f"Training samples:   {stats['train_samples']}")
    print(f"Validation samples: {stats['val_samples']}")
    print(f"Total IQ data:      {stats['total_iq_bytes'] / 1024 / 1024:.1f} MB")
    print(f"Total frame data:   {stats['total_frame_bytes'] / 1024 / 1024:.1f} MB")
    print(f"\nDevice coverage:")
    for device, count in stats['devices_coverage'].items():
        print(f"  {device}: {count} samples")
    print(f"\nOutput: {output_dir}")
    print(f"\nTo upload to S3:")
    print(f"  aws s3 sync {output_dir} s3://your-bucket/wifi-camera-data/")


if __name__ == "__main__":
    main()

