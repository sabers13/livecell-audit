"""LIVECell Audit MVP: a small, repeatable data-quality audit for the LIVECell COCO splits.

Reads the three official COCO annotation files by streaming (never holding a
whole file in memory), reconciles image references with local TIFF files,
reports within-split repeats and cross-split filename overlap, applies a
simple low-annotation-count baseline, and writes deterministic report data,
per-finding evidence, checksum manifests, and a derived validation membership
manifest.

Only the Python standard library plus Pillow (for image dimensions and report
rendering) are required.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
