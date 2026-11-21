"""项目入口包装：在 ``python -I`` 隔离模式下显式加入项目根后调用 CLI。

用法（在项目根目录）：
    python -I run.py ingest
    python -I run.py run-all --run-id current
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
