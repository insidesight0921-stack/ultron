"""
pytest fixtures + sys.path 세팅.

scripts/ 모듈을 그대로 import할 수 있게 한다.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
