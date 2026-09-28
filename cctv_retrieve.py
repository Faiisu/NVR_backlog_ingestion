#!/usr/bin/env python3
"""Compatibility entry point for the NVR ingestion CLI."""
import sys
from nvr_ingestion import core as _core

if __name__ == "__main__":
    _core.main()
else:
    # Preserve the historical import surface, including monkeypatchable globals.
    sys.modules[__name__] = _core
