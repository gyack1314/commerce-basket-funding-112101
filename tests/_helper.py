"""测试共享引导：保证 ``python -I -m unittest`` 下能导入 app 与合成资料。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import FactStore, parse_ts  # noqa: E402

DATA = [str(ROOT / "data" / "history.json"), str(ROOT / "data" / "updates.json")]
LATE_CUTOFF = "2026-07-15T00:00:00+00:00"


def load_store() -> FactStore:
    return FactStore.load(DATA)


def ts(value: str = LATE_CUTOFF):
    return parse_ts(value)
