"""近似开关。色卡留出的平均 ΔE00 记在报告里，这个测试不重算。"""

from __future__ import annotations

from gxr_studio.finish.approx import ENABLED


def test_approx_switch_stays_off() -> None:
    """开关本身。色卡留出的 ΔE00 记在报告里，这里不重算。"""
    assert ENABLED is False
