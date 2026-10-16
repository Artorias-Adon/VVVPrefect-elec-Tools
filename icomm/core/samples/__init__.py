"""内置示例工程。

一个示例一个模块，写法统一（都用 :mod:`. _common` 的构造助手）：

    bess.py    储能电站（BESS / PCS + 电池簇）
    pv.py      光伏电站（组串 + 逆变器 + 环境监测）
    grid.py    电力系统（配电网：变电站 + 馈线 + 配变监测）

每个示例模块都要提供：

* ``NAME``          界面上显示的工程名（中文）
* ``FOLDER_NAME``   磁盘文件夹名（英文 ASCII，见 :func:`icomm.core.project_store.folder_name`）
* ``UNIT``/``UNITS`` 用到的 Modbus 从站号
* ``build()``       返回一台可直接采集的 :class:`~icomm.core.models.Project`
* ``simulate(store, t)``  按仿真时间刷新该从站寄存器（供 ``tools/modbus_sim.py`` 用）

**为什么要拆成包**：示例一多，``project.py`` 会变成一个巨大的文件；
而且每个示例的"点表 + 仿真行为"必须成对维护（地址写错就对不上），
放一起才不容易走样。SST 与配电柜两个老示例暂时还留在 ``project.py`` 里。

**依赖方向**：本包只依赖 ``..models``，**不要** import ``..project``，
否则会和 ``project.py`` 的注册形成循环导入。
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Callable

from . import bess, grid, pv

__all__ = [
    "MODULES",
    "NAME_TO_MODULE",
    "REGISTRY",
    "SIM_UNIT_IDS",
    "SIM_UNITS",
    "build_all",
    "module_for_unit",
]

#: 所有示例模块（顺序 = 示例菜单里的顺序；SST 与配电柜由 project.py 追加在后面）
MODULES: tuple[ModuleType, ...] = (bess, pv, grid)

#: ``{工程名: 构造函数}``，直接被 ``project.BUILTIN_SAMPLES`` 合并
REGISTRY: dict[str, Callable[[], Any]] = {module.NAME: module.build for module in MODULES}

#: ``{工程名: 模块}``
NAME_TO_MODULE: dict[str, ModuleType] = {module.NAME: module for module in MODULES}


def _units_of(module: ModuleType) -> tuple[int, ...]:
    """一个模块声明的从站号（``UNITS`` 优先，其次 ``UNIT``）。"""
    units = getattr(module, "UNITS", None)
    if units:
        return tuple(int(unit) for unit in units)
    unit = getattr(module, "UNIT", None)
    return (int(unit),) if unit else ()


def sim_units() -> list[tuple[int, ModuleType]]:
    """``[(从站号, 示例模块), ...]``：仿真器要建数据区并每轮刷新的从站。"""
    pairs: list[tuple[int, ModuleType]] = []
    for module in MODULES:
        for unit in _units_of(module):
            pairs.append((unit, module))
    return pairs


#: 仿真器要在启动时建好数据区的从站号
SIM_UNIT_IDS: tuple[int, ...] = tuple(unit for unit, _module in sim_units())

#: 同 ``sim_units()``，只是包成元组方便外部直接遍历
SIM_UNITS: tuple[tuple[int, ModuleType], ...] = tuple(sim_units())


def module_for_unit(unit_id: int) -> ModuleType | None:
    """这个从站号归哪个示例管（仿真器分派用）。"""
    for unit, module in SIM_UNITS:
        if unit == unit_id:
            return module
    return None


def build_all() -> dict[str, Any]:
    """把每个示例都建一遍（自检用：能跑通说明点表没写错）。"""
    return {name: factory() for name, factory in REGISTRY.items()}
