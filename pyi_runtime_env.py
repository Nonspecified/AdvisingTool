"""PyInstaller runtime env guard for pandas/numpy on macOS."""

import os
import sys

# Force cmath import so PyInstaller bundles it.
import cmath

# Remove host Python/Conda variables that can break frozen imports.
for key in ("PYTHONHOME", "PYTHONPATH", "_PYTHON_HOST_PLATFORM"):
    os.environ.pop(key, None)

# On macOS, force a valid sysconfigdata module name expected by stdlib.
if sys.platform == "darwin":
    os.environ["_PYTHON_SYSCONFIGDATA_NAME"] = "_sysconfigdata__darwin_darwin"
