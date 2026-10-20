from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="analystbot-tests-"))
atexit.register(shutil.rmtree, _TEST_ROOT, True)

os.environ["ANALYSTBOT_ROOT"] = str(_TEST_ROOT)
os.environ["ANALYSTBOT_LOG_TO_FILE"] = "0"
