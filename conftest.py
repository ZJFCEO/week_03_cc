import sys
from pathlib import Path

# 让 `pytest` 和 `python -m pytest` 都能导入根目录下的 gate.py / harness.py 等模块。
ROOT = str(Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
