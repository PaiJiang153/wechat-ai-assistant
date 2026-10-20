"""pytest 引导：保证从任意目录运行时都能 import src / web。"""
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolate_runtime_writes(tmp_path, monkeypatch):
    """Relative default data/log paths must never write into a live workspace."""
    monkeypatch.chdir(tmp_path)
