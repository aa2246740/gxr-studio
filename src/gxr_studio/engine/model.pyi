"""类型存根。model.py 的搬来的方法与研究代码逐段锁定，注解只放在这里。"""

from typing import Any

import numpy as np
from numpy.typing import NDArray

from .matrix_model import MatrixRow
from .pretable import PreTable

class _Ctx:
    stock: NDArray[np.int64]
    d0: NDArray[np.float64]
    d1: NDArray[np.float64]
    S_eff: NDArray[np.complex128]

class _MatrixApi:
    MatrixRow: type[MatrixRow]
    @staticmethod
    def ycbcr(rgb: Any) -> tuple[NDArray[np.float64], NDArray[np.complex128]]: ...
    @staticmethod
    def rgb_from_ycc(y: Any, c: Any) -> NDArray[np.float64]: ...

class _ColourApi:
    M709: NDArray[np.float64]
    D65: NDArray[np.float64]
    @staticmethod
    def Z_of_words(A: Any, B: Any, ctx: Any) -> NDArray[np.complex128]: ...
    @staticmethod
    def lab_from_codes(rgb: Any) -> NDArray[np.float64]: ...
    @staticmethod
    def delta_e00(lab1: Any, lab2: Any) -> NDArray[np.float64]: ...
    @staticmethod
    def srgb_to_linear(v: Any) -> NDArray[np.float64]: ...

class _Engine:
    mm: _MatrixApi
    fs: _ColourApi
    ctx: _Ctx
    table: PreTable

class Model:
    engine: _Engine
    mm: _MatrixApi
    fs: _ColourApi
    def __init__(self, stock_words: Any, d0: Any, d1: Any, ring_model: dict[str, Any]) -> None: ...
    def pre(self, b: bytes | bytearray, x: Any, channel_mode: bool | None = ...) -> NDArray[np.float64]: ...
    def predict(
        self,
        b: bytes | bytearray,
        x: Any,
        channel_mode: bool | None = ...,
        quantize: bool = ...,
        half: int = ...,
        bw_chroma: str = ...,
    ) -> NDArray[np.float64]: ...
    def lab(self, rgb: Any) -> NDArray[np.float64]: ...
    def de(self, a: Any, b: Any) -> NDArray[np.float64]: ...

def synthetic() -> NDArray[np.float64]: ...
def recipe_chart(model: Model, base: bytes | bytearray) -> NDArray[np.float64]: ...
def masks(model: Model, base: bytes | bytearray, x: Any, param: str, chart: bool = ...) -> list[NDArray[np.bool_]]: ...
