"""墙钟速度断言。速度预算是产品要求（跟手优先），这里不放宽，只把它和功能断言分开。

速度没达标时抛 `SpeedBudgetExceeded`（`AssertionError` 的子类，类上带 `gxr_speed_budget = True`）。
`scripts/run_tests.py` 只认这个类型：exclusive 用例只因它失败时，等机器安静后在 exclusive 阶段重跑一次，
以第二次为准。其他失败一律不重跑。runner 不解析断言文字。

`speed_budgets()` 把一段里的速度检查攒到最后：这段里的功能断言先失败时，抛出的是功能断言（不重跑），
没过的速度预算作为附注写在后面；功能断言全过、只有速度没过时，才抛 `SpeedBudgetExceeded`。
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager


class SpeedBudgetExceeded(AssertionError):
    """一项或几项墙钟预算没达到。"""

    gxr_speed_budget = True


def _shown(value: float | None) -> str:
    return "没有在等待窗口内出现" if value is None else f"{value:g}"


class SpeedBudgets:
    """一段测试里的速度预算。值是 None 表示在驱动脚本的等待窗口里都没等到，算超出预算。"""

    def __init__(self) -> None:
        self.missed: list[str] = []

    def _check(self, ok: bool, line: str) -> None:
        if not ok:
            self.missed.append(line)

    def below(self, value: float | None, budget: float, what: str) -> None:
        """value < budget。"""
        self._check(value is not None and not math.isnan(value) and value < budget, f"{what}：{_shown(value)}，要 < {budget:g}")

    def at_most(self, value: float | None, budget: float, what: str) -> None:
        """value <= budget。"""
        self._check(value is not None and not math.isnan(value) and value <= budget, f"{what}：{_shown(value)}，要 <= {budget:g}")

    def at_least(self, value: float | None, floor: float, what: str) -> None:
        """value >= floor（比如每秒次数的下限）。"""
        self._check(value is not None and not math.isnan(value) and value >= floor, f"{what}：{_shown(value)}，要 >= {floor:g}")

    def raise_if_missed(self) -> None:
        if self.missed:
            raise SpeedBudgetExceeded("墙钟预算没达到：" + "；".join(self.missed))


@contextmanager
def speed_budgets() -> Iterator[SpeedBudgets]:
    budgets = SpeedBudgets()
    try:
        yield budgets
    except BaseException as exc:
        if budgets.missed and not isinstance(exc, SpeedBudgetExceeded):
            exc.add_note("另外这些墙钟预算也没达到：" + "；".join(budgets.missed))
        raise
    budgets.raise_if_missed()


def assert_fast(value: float | None, budget: float, what: str) -> None:
    """马上检查 value < budget，没过就抛 SpeedBudgetExceeded。"""
    budgets = SpeedBudgets()
    budgets.below(value, budget, what)
    budgets.raise_if_missed()
