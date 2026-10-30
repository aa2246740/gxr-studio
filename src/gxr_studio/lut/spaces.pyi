"""类型存根。spaces.py 与研究代码逐字节锁定，注解只放在这里。"""

from typing import Any, Literal, overload

import numpy as np
from numpy.typing import NDArray

PRIMARIES: dict[str, tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]
GAMUT: dict[str, str]
DISPLAY: tuple[str, ...]
SPACES: tuple[str, ...]

def matrix(gamut: str) -> NDArray[np.float64]: ...
def log10(x: Any) -> NDArray[np.float64]: ...
def encode(x: Any, space: str) -> NDArray[np.float64]: ...
def decode(x: Any, space: str) -> NDArray[np.float64]: ...
def xyz_to_encoded(xyz: Any, space: str, gamut: str | None = ...) -> NDArray[np.float64]: ...
def to_srgb(encoded: Any, space: str = ..., gamut: str | None = ...) -> NDArray[np.float64]: ...

class Target:
    cube: Any
    input_space: str
    output_space: str
    input_gamut: str | None
    output_gamut: str | None
    input_range: str
    output_range: str
    def __init__(
        self,
        cube: Any,
        input_space: str,
        output_space: str = ...,
        input_gamut: str | None = ...,
        output_gamut: str | None = ...,
        input_range: str = ...,
        output_range: str = ...,
    ) -> None: ...
    @overload
    def render(self, stock_rgb: Any, xyz: Any = ..., info: Literal[False] = ...) -> NDArray[np.float64]: ...
    @overload
    def render(self, stock_rgb: Any, xyz: Any = ..., *, info: Literal[True]) -> tuple[NDArray[np.float64], dict[str, float]]: ...
    def description(self) -> dict[str, str]: ...
