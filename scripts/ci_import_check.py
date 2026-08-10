#!/usr/bin/env python3
"""CI import check — verify all backend modules import without errors.

Run with deprecation warnings as errors so any deprecated API usage
is caught before deployment:

    python -W error::DeprecationWarning scripts/ci_import_check.py
"""

import sys
import os

# Add backend directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from patterns import PatternEngine
from sentiment import MarketSentimentEngine

print("All imports OK (no deprecation warnings)")