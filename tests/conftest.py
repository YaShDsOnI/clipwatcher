"""Tests run against a throw-away data folder, never the real clips/ or .env."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_HOME = Path(tempfile.mkdtemp(prefix="clip-watcher-test-"))
os.environ["CLIP_WATCHER_HOME"] = str(_HOME)        # must be set before clipwatcher is imported
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def home():
    """An empty data folder for one test."""
    for child in _HOME.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    for key in ("KEEP_SOURCES_DAYS", "KEEP_CLIPS_DAYS"):
        os.environ.pop(key, None)
    return _HOME


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_HOME, ignore_errors=True)
