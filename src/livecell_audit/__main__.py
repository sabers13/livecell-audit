"""Allow ``PYTHONPATH=src python3 -m livecell_audit audit ...``."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
