"""类型存根。menu.py 与研究代码只差两行导入，注解只放在这里。"""

from pathlib import Path
from typing import Any

from .knobs import Request

TABLE_PATH: Path

def load() -> dict[str, Any]: ...
def request(data: dict[str, Any], unit: int, slot: int, levels: list[int] | tuple[int, ...]) -> Request: ...
