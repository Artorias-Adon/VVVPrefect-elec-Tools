# 第三方组件与许可（THIRD-PARTY LICENSES）

本软件 **VVPhmi**（Python 包名 `icomm`）以 **GPL-3.0-or-later** 发布，见 [LICENSE](./LICENSE)。

下面列出运行/打包时用到的第三方组件及其许可。**本仓库不分发这些组件的源码**，
安装时由 pip 从各自官方源获取；发布 zip 里带着的是它们的运行形态（DLL / 已编译字节码），
因此各家的署名与许可要求在这里一并说明 —— 再分发本软件时请把这一份一起带上。

## 运行时依赖

| 组件 | 许可 | 用途 | 合规要点 |
|---|---|---|---|
| **PySide6 / shiboken6**（Qt for Python） | **LGPL-3.0-only** 或 GPL-2.0-only 或 GPL-3.0-only（三选一） | 图形界面 | 按 **LGPL-3.0** 使用：未修改其源码、以独立文件形式随程序分发（`_internal` 里的 Qt DLL 单独存放），用户可自行替换；再分发时需带 LGPL 文本与版权声明 |
| **python-can** | **LGPL-3.0-only** | CAN 总线 | 同上：未修改、独立文件分发、可替换 |
| **pymodbus** | BSD-3-Clause | Modbus TCP/RTU | 保留版权声明与免责声明即可 |
| **pyserial** | BSD-3-Clause | 串口（RTU） | 同上 |
| **python-snap7** | MIT | 西门子 S7 | 纯 Python 实现，无原生库；保留版权声明 |
| **iec61850**（`iec61850-python`，csp0924） | **MIT** 或 Apache-2.0（双许可） | IEC 61850 MMS | 二选一遵守；本项目按 MIT 处理，保留版权声明 |

> 说明：本软件**没有**链接 GPL 版的 `libiec61850`；61850 走的是上面这个 MIT/Apache 双许可的 Python 实现。

## 仅打包时使用的工具

| 工具 | 许可 | 说明 |
|---|---|---|
| **PyInstaller** | GPL-2.0 **with bootloader exception** | 其许可明确允许把打包产出的可执行文件按**你自己的协议**发布；本软件用它打出 `VVPhmi.exe`，不受 GPL-2.0 传染 |

## 图标与素材

| 素材 | 来源 | 许可 |
|---|---|---|
| `assets/icon.*` | 由本仓库的 `tools/make_icon.py` 用代码绘制生成 | 随本项目，GPL-3.0-or-later |
| `assets/sim_iec61850.icd` | 本仓库自写的**最小示例** ICD | 随本项目，GPL-3.0-or-later |

## 示例工程与示例数据

`examples/` 里的点表模板、`icomm/core/samples/` 里的示例工程（储能 / 光伏 / SST / 电力系统 / 配电柜）
以及 `tools/*_sim.py` 里的仿真数据，**全部是本仓库自己编写的示意数据**：
设备名、点号、地址、量程都是为了演示编的，不含任何客户点表、现场图纸或真实装置参数。
导入真实点表的人请自行确认那份点表的归属与保密要求。

## 再分发时的最小要求（给打包给别人用的人）

1. 带上 `LICENSE`（GPL-3.0 全文）与这份 `THIRD-PARTY-LICENSES.md`；
2. 如果你的版本改了源码，按 GPL-3.0 提供对应源码；
3. 保留 Qt/PySide6 与 python-can 的 LGPL 声明，并保持它们以独立文件形式可替换
   （本项目就是 `dist/VVPhmi/_internal/` 这种一目录结构，别把它们塞进单文件自解压包里，
   那样会破坏"用户可替换"这一条）。

获取各组件完整许可文本：

- PySide6 / Qt：<https://www.qt.io/licensing> 、<https://www.gnu.org/licenses/lgpl-3.0.txt>
- python-can：<https://github.com/hardbyte/python-can/blob/main/LICENSE.txt>
- pymodbus：<https://github.com/pymodbus-dev/pymodbus/blob/dev/LICENSE>
- pyserial：<https://github.com/pyserial/pyserial/blob/master/LICENSE.txt>
- python-snap7：<https://github.com/gijzelaerr/python-snap7/blob/master/LICENSE>
- iec61850-python：<https://github.com/csp0924/iec61850-python>
- PyInstaller：<https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt>
