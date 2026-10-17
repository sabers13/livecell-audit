"""Error types for the LIVECell audit."""

from __future__ import annotations


class AuditError(Exception):
    """Raised when source data fails required-structure validation.

    Carries a human-readable message that names the file and the location
    (top-level key, array name, element index) where validation failed.
    """
