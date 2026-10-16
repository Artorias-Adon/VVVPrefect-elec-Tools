"""串口枚举。

独立成一个小模块的原因：**枚举串口这件事和协议无关**（Modbus RTU 要用，
以后加 DL/T 645、IEC 104 串口版也要用），而且拿不到 pyserial 时也不能让程序起不来。

界面上的用法：串口号做成"可下拉可选、也能手打"的输入框，
每次打开下拉都重新枚举一遍 —— 现场插拔 USB 转串口设备很频繁，
不重启程序就能看到新串口才顺手。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SerialPortInfo:
    """一个串口。"""

    device: str
    description: str = ""

    def label(self) -> str:
        return f"{self.device}　{self.description}".strip()


def list_serial_ports() -> list[SerialPortInfo]:
    """枚举系统上的串口。

    **绝不抛异常**：没有 pyserial、驱动异常、权限不足都只是返回空列表，
    界面仍然允许手打串口名（现场有些板子起的虚拟串口枚举不出来，只能手填）。
    """
    try:
        from serial.tools import list_ports  # 延迟导入：缺 pyserial 时其它功能照常
    except Exception:  # noqa: BLE001 - 任何导入问题都按"枚举不出来"处理
        return []
    try:
        found = list_ports.comports()
    except Exception:  # noqa: BLE001
        return []
    ports: list[SerialPortInfo] = []
    for item in found:
        device = str(getattr(item, "device", "") or "").strip()
        if not device:
            continue
        description = str(getattr(item, "description", "") or "").strip()
        ports.append(SerialPortInfo(device=device, description=description))
    return sorted(ports, key=lambda port: _sort_key(port.device))


def _sort_key(device: str) -> tuple:
    """COM10 排在 COM9 后面（按数字排，不是按字符串）。"""
    upper = device.upper()
    if upper.startswith("COM") and upper[3:].isdigit():
        return (0, int(upper[3:]), device)
    return (1, 0, device)


def port_names() -> list[str]:
    return [port.device for port in list_serial_ports()]


def describe(device: str) -> str:
    """某个串口的描述（找不到返回空串）。"""
    wanted = (device or "").strip().upper()
    for port in list_serial_ports():
        if port.device.upper() == wanted:
            return port.description
    return ""


def summary() -> str:
    """一行说明当前枚举到的串口（日志/状态栏用）。"""
    ports = list_serial_ports()
    if not ports:
        return "没有枚举到串口（可以手填串口号，例如 COM3）"
    return "枚举到 " + "、".join(port.label() for port in ports[:6]) + (
        "…" if len(ports) > 6 else ""
    )


#: 常见波特率（界面下拉用）
BAUDRATES: tuple[int, ...] = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400)

#: 校验位 → 中文说明
PARITY_LABELS = {
    "N": "无校验(N)",
    "E": "偶校验(E)",
    "O": "奇校验(O)",
}

#: 最常用的默认参数：9600 / 8 / 偶校验 / 1 停止位
DEFAULT_PORT = "COM1"
DEFAULT_BAUDRATE = 9600
DEFAULT_PARITY = "E"
DEFAULT_BYTESIZE = 8
DEFAULT_STOPBITS = 1


def frame_text(baudrate: int, bytesize: int, parity: str, stopbits: int) -> str:
    """把串口参数写成 ``9600 8E1`` 这种现场说法。"""
    return f"{int(baudrate)} {int(bytesize)}{parity}{int(stopbits)}"
