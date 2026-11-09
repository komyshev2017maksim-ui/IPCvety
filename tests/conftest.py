import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Каждый тест работает в своей пустой папке данных."""
    monkeypatch.setenv("ANTIDETECT_HOME", str(tmp_path / "home"))
    if "ANTIDETECT_BROWSER" not in os.environ:
        # В контейнерах разработки Chromium лежит по этому пути.
        for candidate in ("/opt/pw-browsers/chromium", shutil.which("chromium") or ""):
            if candidate and Path(candidate).exists():
                monkeypatch.setenv("ANTIDETECT_BROWSER", candidate)
                break
    yield
