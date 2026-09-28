#!/usr/bin/env python3
"""Compatibility entry point for the NVR ingestion web GUI."""
import sys
from nvr_ingestion.web import app as _app

if __name__ == "__main__":
    _app.main()
else:
    # Preserve the historical import surface used by deployments and callers.
    sys.modules[__name__] = _app
