"""Error types for the LIVECell audit."""

from __future__ import annotations


class AuditError(Exception):
    """Raised when source data fails required-structure validation.

    Carries a human-readable message that names the file and the location
    (top-level key, array name, element index) where validation failed.
    """


class WarehouseError(Exception):
    """Raised when a warehouse build cannot proceed.

    Raised for a missing or invalid completion marker, a source checksum or
    summary-count mismatch, an inconsistent audit export, or a DuckDB failure.
    The message names the concrete file or check that failed.
    """


class FeatureError(Exception):
    """Raised when the feature stage cannot proceed.

    Raised for a missing warehouse completion marker, an inconsistent
    annotation/image join, an unresolved key relationship, or a TIFF read
    contract violation. A failed feature build never leaves a completed
    output directory behind.
    """
