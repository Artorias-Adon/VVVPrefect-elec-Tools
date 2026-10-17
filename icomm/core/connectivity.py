"""连通性测试：按协议真连一次，并把"通 / 通了但没人答 / 答了但拒绝"分清。

现场判断"这条线到底通不通"，光 ping 不够，光看 TCP 端口也不够：

* **连不上** —— IP / 端口 / 串口打不开：线、供电、防火墙、波特率的事；
* **连上了但对方不答** —— TCP 端口通、Modbus 从站不回：站号错、协议不匹配、网关没透传；
* **答了但拒绝这个地址** —— 通信本身是通的，只是拿来做测试的地址不可用。

所以这里**不另写一套协议探针**，而是直接拿驱动真连一次：能连上、还能读回一个值，
就等价于"采集能跑起来"。这样每种协议（含以后新加的）自动都有连通性测试，
而且测出来的结论和真正采集时的行为一致 —— 不会出现"测试通过、采集却不通"。

界面只管显示，逻辑全在这里（不依赖 Qt，可单测、可命令行复用）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .drivers import DriverError, OptionField, create_driver, default_options
from .models import ChannelDef, DeviceDef, PointDef
from .net_check import ConnectionTester, format_bytes, parse_hex_payload
from .net_check import _suggestions as _network_suggestions

#: CAN 被动监听时长（秒）：CAN 没有"握手"，只能靠"能不能收到帧"判断总线活没活
CAN_LISTEN_S = 3.0

#: 明细里最多保留多少条报文
MAX_FRAMES = 60

STEP_OK = "ok"
STEP_WARN = "warn"
STEP_FAIL = "fail"

#: 明细里的状态记号。都用 GBK 里也有的字符，免得现场把结果存成文本后在
#: 命令行/记事本里显示成乱码。
_STEP_MARKS = {STEP_OK: "√", STEP_WARN: "!", STEP_FAIL: "×"}


# --------------------------------------------------------------- 协议清单


@dataclass(frozen=True)
class ProtocolSpec:
    """一种可测的通信方式。

    ``driver`` 为空表示"裸 TCP"（只测端口，不发协议报文）。
    """

    key: str
    label: str
    driver: str
    link: str
    hint: str
    #: 测试时要填从站号（一条链路上有多台从站）
    needs_unit: bool = False


PROTOCOLS: tuple[ProtocolSpec, ...] = (
    ProtocolSpec(
        "tcp",
        "TCP 端口",
        "",
        "tcp",
        "只测 IP + 端口通不通，可手工发一串字节看回应",
    ),
    ProtocolSpec(
        "modbus_tcp",
        "Modbus TCP",
        "modbus",
        "tcp",
        "连上后真读一个保持寄存器：能读回值才算通",
        needs_unit=True,
    ),
    ProtocolSpec(
        "modbus_rtu",
        "Modbus RTU (串口)",
        "modbus",
        "rtu",
        "按波特率/校验位打开串口，再真读一个保持寄存器",
        needs_unit=True,
    ),
    ProtocolSpec(
        "modbus_rtu_over_tcp",
        "Modbus RTU over TCP",
        "modbus",
        "rtu_over_tcp",
        "串口服务器/透传网关：TCP 上跑 RTU 帧",
        needs_unit=True,
    ),
    ProtocolSpec(
        "can",
        "CAN 总线",
        "can",
        "can",
        "打开适配器并被动监听几秒：收到帧说明总线是活的",
    ),
    ProtocolSpec(
        "iec61850",
        "IEC 61850 (MMS)",
        "iec61850",
        "tcp",
        "建立 MMS 关联并读模型目录（逻辑设备列表）",
    ),
    ProtocolSpec(
        "s7",
        "西门子 S7",
        "s7",
        "tcp",
        "ISO-on-TCP 连接后读一个 M 区位，验证机架/插槽与 PUT/GET 权限",
    ),
)

_BY_KEY = {spec.key: spec for spec in PROTOCOLS}


# --------------------------------------------------------------- 结果结构


@dataclass
class CheckStep:
    """测试中的一步（打开连接 / 读一个值 / 收帧 …）。"""

    label: str
    level: str = STEP_OK
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.level != STEP_FAIL


@dataclass
class CheckResult:
    """一次连通性测试的结论。"""

    key: str = "tcp"
    target: str = ""
    steps: list[CheckStep] = field(default_factory=list)
    frames: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def label(self) -> str:
        spec = _BY_KEY.get(self.key)
        return spec.label if spec else self.key

    @property
    def failed(self) -> list[CheckStep]:
        return [step for step in self.steps if step.level == STEP_FAIL]

    @property
    def warnings(self) -> list[CheckStep]:
        return [step for step in self.steps if step.level == STEP_WARN]

    @property
    def ok(self) -> bool:
        return bool(self.steps) and not self.failed

    @property
    def headline(self) -> str:
        """一句话结论，界面直接显示这行。"""
        if not self.steps:
            return f"{self.label} 没有执行测试"
        if self.failed:
            return f"{self.label} 通信失败：{self.target} —— {self.failed[0].detail}"
        text = f"{self.label} 通信正常：{self.target}"
        if self.warnings:
            text += f"（有 {len(self.warnings)} 项提示）"
        return text

    def detail(self) -> str:
        """多行明细：逐步结论 + 报文 + 排查建议。"""
        lines: list[str] = []
        for step in self.steps:
            mark = _STEP_MARKS.get(step.level, "·")
            lines.append(f"[{mark}] {step.label}：{step.detail}" if step.detail else f"[{mark}] {step.label}")
        if self.elapsed_ms:
            lines.append("")
            lines.append(f"总耗时 {self.elapsed_ms:.1f} ms")
        if self.frames:
            lines.append("")
            lines.append("报文：")
            lines.extend(f"  {frame}" for frame in self.frames)
        if self.hints:
            lines.append("")
            lines.append("排查建议：")
            lines.extend(self.hints)
        return "\n".join(lines)

    def as_text(self) -> str:
        """存档用：结论 + 明细。"""
        return f"{self.headline}\n\n{self.detail()}\n"


# --------------------------------------------------------------- 参数辅助


def protocol_keys() -> list[str]:
    return [spec.key for spec in PROTOCOLS]


def protocol_spec(key: str) -> ProtocolSpec:
    spec = _BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"未知的通信方式「{key}」，可选：{', '.join(protocol_keys())}")
    return spec


def spec_for_channel(driver: str, link: str) -> ProtocolSpec | None:
    """工程通道的 (驱动, 链路) 对应哪种可测通信方式（对不上返回 None）。"""
    for spec in PROTOCOLS:
        if spec.driver == driver and spec.link == link:
            return spec
    return None


def option_fields(key: str) -> list[OptionField]:
    """该通信方式的连接参数表单定义（驱动声明的，和通道编辑里完全一致）。"""
    spec = protocol_spec(key)
    if not spec.driver:
        return [
            OptionField("host", "IP 地址", "str", "127.0.0.1"),
            OptionField("port", "端口", "int", 502, minimum=1, maximum=65535),
        ]
    driver_cls = _driver_class(spec.driver)
    if driver_cls is None:
        return []
    return list(driver_cls.option_schema(spec.link))


def default_options_for(key: str) -> dict[str, Any]:
    spec = protocol_spec(key)
    if not spec.driver:
        return {"host": "127.0.0.1", "port": 502}
    return dict(default_options(spec.driver, spec.link))


def target_text(key: str, options: dict[str, Any]) -> str:
    """把参数写成一句人话，例如 ``192.168.1.5:502`` 或 ``COM3 9600 8E1``。"""
    spec = protocol_spec(key)
    options = options or {}
    if spec.key == "can":
        return f"{options.get('backend', 'slcan')}@{options.get('channel', '')}"
    if spec.link == "rtu":
        return (
            f"{options.get('port', '')} {options.get('baudrate', '')}"
            f" {options.get('bytesize', 8)}{options.get('parity', 'N')}{options.get('stopbits', 1)}"
        )
    host = str(options.get("host", "") or "").strip() or "（未填地址）"
    return f"{host}:{options.get('port', '')}"


def _driver_class(name: str):
    from .drivers import driver_class

    return driver_class(name)


# --------------------------------------------------------------- 排查建议

_LIBRARY_HINTS: dict[str, list[str]] = {
    "modbus_tcp": ["· 缺少 pymodbus：pip install pymodbus（正式安装包里自带）"],
    "modbus_rtu": ["· 缺少 pymodbus：pip install pymodbus（正式安装包里自带）"],
    "modbus_rtu_over_tcp": ["· 缺少 pymodbus：pip install pymodbus（正式安装包里自带）"],
    "can": ["· 缺少 python-can：pip install python-can"],
    "iec61850": [
        "· 缺少 iec61850 库：pip install iec61850",
        "· 该库只有 Windows / Linux 轮子，macOS 上无法使用 61850 协议",
    ],
    "s7": ["· 缺少 python-snap7：pip install python-snap7"],
}

_PROTOCOL_HINTS: dict[str, tuple[str, ...]] = {
    "tcp": (),
    "modbus_tcp": (
        "· 端口通但读不到：先确认从站号（默认 1），站号错了现象和线路不通一模一样。",
        "· 网关/串口服务器要确认「透传」是否开启，以及它后面的 RTU 站号映射。",
        "· 寄存器地址别照抄手册：有的设备 40001 对应地址 0，有的对应 1。",
    ),
    "modbus_rtu": (
        "· 串口能打开但没数据：波特率/校验位/停止位必须和仪表完全一致（9600 8E1 最常见）。",
        "· RS485 接 A/B 两根，反接会完全没数据；总线两端各 120Ω 终端电阻。",
        "· 从站号（默认 1）和仪表菜单里设的地址要一致。",
        "· 串口被别的软件（组态软件、串口助手）占着也打不开，先关掉它。",
    ),
    "modbus_rtu_over_tcp": (
        "· 能用 Modbus TCP 就别用 RTU over TCP：先试「Modbus TCP」，能通就用那个。",
        "· 透传网关要设成「透明传输」，并确认从站号能透过去。",
        "· 波特率/校验位要和网关串口侧、仪表侧都一致。",
    ),
    "can": (
        "· 波特率整条总线必须一致（500 kbit/s 最常见），改错会一帧都收不到。",
        "· 检查 CAN_H / CAN_L 有没有反接、终端电阻 120Ω、适配器驱动装没装。",
        "· 没有硬件时把后端选「virtual（内置虚拟总线）」可以先验证软件链路。",
        "· slcan 的通道填串口号（COM3 / /dev/ttyUSB0）；PCAN 填 PCAN_USBBUS1。",
    ),
    "iec61850": (
        "· 61850 走 MMS 102 端口，先确认装置 IP 正确、IED 名与 ICD 里一致。",
        "· 装置上要允许客户端连接（有的装置要关掉「仅允许已配置客户端」）。",
        "· 一台装置一个通道：同一装置用两条通道连会争同一个 MMS 关联。",
    ),
    "s7": (
        "· S7 端口固定 102；PLC 要允许 PUT/GET（TIA 里勾选「允许来自远程伙伴的 PUT/GET 通信访问」）。",
        "· 机架/插槽：S7-1200/1500 用 0/1，S7-300 用 0/2，S7-400 常见 0/2~3。",
        "· 被 PLC 的「连接资源」占满时也会连不上，现场可以重启一下 PLC 的以太网口。",
    ),
}


def looks_like_missing_library(error: str) -> bool:
    text = (error or "").lower()
    return any(
        mark in text
        for mark in ("未安装", "不可用", "no module named", "importerror", "not installed")
    )


def suggestions(key: str, error: str) -> list[str]:
    """按协议 + 报错给现场能直接动手的排查建议。"""
    if looks_like_missing_library(error):
        return list(_LIBRARY_HINTS.get(key, []))
    out = list(_PROTOCOL_HINTS.get(key, ()))
    modbus = key.startswith("modbus")
    for line in _network_suggestions(error):
        # 通用建议里有几条是 Modbus 专用的（端口号举例、RTU 串口服务器），
        # 测 S7 / CAN 时列出来反而误导
        if not modbus and ("Modbus" in line or "RTU" in line):
            continue
        if line not in out:
            out.append(line)
    return out


def looks_like_refusal(error: str) -> bool:
    """对方"答了但拒绝"（协议层异常响应）—— 通信其实是通的。"""
    text = (error or "").lower()
    return any(
        mark in text
        for mark in ("异常响应", "非法", "illegal", "exception response", "不支持该功能")
    )


# --------------------------------------------------------------- 测试执行


def run_check(
    key: str,
    options: dict[str, Any],
    *,
    unit_id: int = 1,
    payload: bytes = b"",
    wait_response: bool = True,
    attempts: int = 1,
    read_timeout_s: float = 3.0,
    listen_s: float = CAN_LISTEN_S,
    should_stop: Callable[[], bool] | None = None,
) -> CheckResult:
    """按通信方式真连一次，返回结论。

    ``payload`` / ``wait_response`` / ``attempts`` / ``read_timeout_s`` 只对
    「TCP 端口」这种手工测试有意义；其它协议一律走驱动，参数由 ``options`` 决定。
    """
    spec = protocol_spec(key)
    started = time.perf_counter()
    result = CheckResult(key=spec.key, target=target_text(spec.key, options))
    try:
        if not spec.driver:
            _run_raw_tcp(
                result,
                options,
                payload=payload,
                wait_response=wait_response,
                attempts=attempts,
                read_timeout_s=read_timeout_s,
                should_stop=should_stop,
            )
        else:
            _run_driver(result, spec, options, unit_id, listen_s, should_stop)
    finally:
        result.elapsed_ms = (time.perf_counter() - started) * 1000
    return result


def _run_raw_tcp(
    result: CheckResult,
    options: dict[str, Any],
    *,
    payload: bytes,
    wait_response: bool,
    attempts: int,
    read_timeout_s: float,
    should_stop: Callable[[], bool] | None,
) -> None:
    host = str(options.get("host", "") or "").strip()
    port = int(options.get("port", 502) or 0)
    timeout_s = float(options.get("timeout", 3.0) or 3.0)
    tester = ConnectionTester(
        host=host,
        port=port,
        timeout_s=timeout_s,
        attempts=attempts,
        payload=payload,
        wait_response=wait_response,
        read_timeout_s=read_timeout_s,
    )
    probe = tester.run()
    for item in probe.attempts:
        if not item.ok:
            result.steps.append(CheckStep(f"第 {item.index} 次连接", STEP_FAIL, item.error))
            continue
        parts = [f"耗时 {item.duration_ms:.1f} ms"]
        level = STEP_OK
        if item.sent:
            parts.append(f"发出 {len(item.sent)} 字节：{format_bytes(item.sent)}")
            if item.received:
                parts.append(f"收到 {len(item.received)} 字节：{format_bytes(item.received)}")
            else:
                level = STEP_WARN
                parts.append(f"没收到回应（{item.read_error or '对方没发数据'}）")
        result.steps.append(CheckStep(f"第 {item.index} 次连接", level, "；".join(parts)))

    if probe.attempts and not probe.attempts[0].ok:
        result.hints = suggestions("tcp", probe.attempts[0].error)
    elif probe.ok_count and probe.ok_count < len(probe.attempts):
        result.hints = [
            "· 时通时不常：网线/水晶头接触不良、交换机端口有问题、IP 冲突、网关过载。",
            "· 建议多测几次（把「重复次数」调大）并检查物理链路。",
        ]
    elif any(step.level == STEP_WARN for step in result.steps):
        result.hints = [
            "· 端口通了但对方没回数据：可能不是这个协议的服务，或服务端要求先发握手。",
            "· 用对应协议的测试项（Modbus / S7 / 61850）再测一次更准。",
        ]


def _run_driver(
    result: CheckResult,
    spec: ProtocolSpec,
    options: dict[str, Any],
    unit_id: int,
    listen_s: float,
    should_stop: Callable[[], bool] | None,
) -> None:
    frames: list[str] = []
    logs: list[str] = []
    channel = ChannelDef(
        name=f"{spec.label} 测试", driver=spec.driver, link=spec.link, options=dict(options)
    )
    try:
        driver = create_driver(channel)
    except DriverError as exc:
        result.steps.append(CheckStep("创建驱动", STEP_FAIL, str(exc)))
        result.hints = suggestions(spec.key, str(exc))
        return

    driver.set_callbacks(
        on_frame=lambda direction, text: frames.append(f"{direction} {text}"),
        on_log=lambda level, message: logs.append(f"{level}: {message}"),
    )

    started = time.perf_counter()
    try:
        driver.open()
    except Exception as exc:  # 驱动会抛 DriverError，也可能抛底层库的异常
        error = str(exc) or exc.__class__.__name__
        elapsed = (time.perf_counter() - started) * 1000
        result.steps.append(CheckStep("打开连接", STEP_FAIL, f"{error}（{elapsed:.0f} ms）"))
        result.hints = suggestions(spec.key, error)
        try:
            driver.close()
        except Exception:
            pass
        result.frames = frames[-MAX_FRAMES:]
        return
    elapsed = (time.perf_counter() - started) * 1000
    result.steps.append(CheckStep("打开连接", STEP_OK, f"{result.target}（{elapsed:.0f} ms）"))

    try:
        _probe_traffic(result, spec, driver, channel, unit_id, listen_s, logs, frames, should_stop)
    finally:
        try:
            driver.close()
        except Exception:
            pass
        result.frames = frames[-MAX_FRAMES:]


def _probe_traffic(
    result: CheckResult,
    spec: ProtocolSpec,
    driver: Any,
    channel: ChannelDef,
    unit_id: int,
    listen_s: float,
    logs: list[str],
    frames: list[str],
    should_stop: Callable[[], bool] | None,
) -> None:
    if spec.key in ("modbus_tcp", "modbus_rtu", "modbus_rtu_over_tcp"):
        device = DeviceDef(name="连通性测试", channel=channel.name, unit_id=max(1, int(unit_id)))
        point = PointDef(name="probe", register="holding", address=0, data_type="uint16")
        _read_one(
            result,
            logs,
            lambda: driver.read_points(device, [point]),
            "读保持寄存器 0",
            refusal_is_ok=True,
        )
    elif spec.key == "s7":
        device = DeviceDef(name="连通性测试", channel=channel.name)
        point = PointDef(name="probe", register="m", path="M0.0", data_type="bool")
        _read_one(
            result,
            logs,
            lambda: driver.read_points(device, [point]),
            "读位存储区 M0.0",
            refusal_is_ok=True,
            warn_only=True,
        )
    elif spec.key == "iec61850":
        try:
            names = driver.browse_server()
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            result.steps.append(
                CheckStep("读模型目录", STEP_WARN, f"MMS 已连上，但读逻辑设备失败：{error}")
            )
            result.hints = suggestions(spec.key, error)
        else:
            shown = "、".join(str(name) for name in names[:8]) or "（空）"
            result.steps.append(
                CheckStep("读模型目录", STEP_OK, f"{len(names)} 个逻辑设备：{shown}")
            )
    elif spec.key == "can":
        _listen_can(result, spec, listen_s, frames, should_stop)


def _read_one(
    result: CheckResult,
    logs: list[str],
    action: Callable[[], dict[str, Any]],
    label: str,
    *,
    refusal_is_ok: bool = False,
    warn_only: bool = False,
) -> None:
    """读一个点位，把"读到 / 答了但拒绝 / 完全没答"三种情况分开报。"""
    before = len(logs)
    try:
        outcomes = action()
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
    else:
        good = [item for item in outcomes.values() if getattr(item, "ok", False)]
        if good:
            value = good[0].value
            text = "读到值" + (f" {value}" if value is not None else "")
            if isinstance(value, bool):
                text = "读到值 " + ("1" if value else "0")
            result.steps.append(CheckStep(label, STEP_OK, text))
            return
        error = _error_from_logs(logs[before:]) or "没有读到数据"

    if refusal_is_ok and looks_like_refusal(error):
        result.steps.append(
            CheckStep(
                label,
                STEP_WARN,
                f"从站回了异常响应（{error}）—— 通信已通，但测试地址/站号可能不对",
            )
        )
        return
    level = STEP_WARN if warn_only else STEP_FAIL
    result.steps.append(CheckStep(label, level, f"没有读到数据：{error}"))
    if error:
        result.hints = suggestions(result.key, error)


def _error_from_logs(entries: list[str]) -> str:
    """驱动把真实失败原因写在日志里，点位上只有"读取失败"，所以从日志捞。"""
    for line in reversed(entries):
        if line.startswith("error:"):
            text = line[len("error:") :].strip()
            # 形如 "S7.M0.0 读取失败：xxx"，取冒号后的原因更干净
            if "：" in text:
                return text.split("：", 1)[1].strip()
            return text
    return ""


def _listen_can(
    result: CheckResult,
    spec: ProtocolSpec,
    listen_s: float,
    frames: list[str],
    should_stop: Callable[[], bool] | None,
) -> None:
    """CAN 没有握手，只能被动听：收到帧 = 总线活的、适配器工作正常。"""
    wait = max(0.2, float(listen_s))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if frames:
            break
        if should_stop is not None and should_stop():
            break
        time.sleep(0.05)
    if frames:
        sample = frames[0]
        extra = f"，共 {len(frames)} 帧" if len(frames) > 1 else ""
        result.steps.append(CheckStep("监听总线", STEP_OK, f"收到帧：{sample}{extra}"))
        return
    result.steps.append(
        CheckStep(
            "监听总线",
            STEP_WARN,
            f"适配器已打开，但 {wait:.1f} 秒内没收到任何帧（总线空闲、没接设备，或波特率不对）",
        )
    )
    result.hints = list(_PROTOCOL_HINTS.get(spec.key, ()))


# --------------------------------------------------------------- 参数解析转发

__all__ = [
    "CAN_LISTEN_S",
    "CheckResult",
    "CheckStep",
    "PROTOCOLS",
    "ProtocolSpec",
    "STEP_FAIL",
    "STEP_OK",
    "STEP_WARN",
    "default_options_for",
    "looks_like_missing_library",
    "looks_like_refusal",
    "option_fields",
    "parse_hex_payload",
    "protocol_keys",
    "protocol_spec",
    "run_check",
    "spec_for_channel",
    "suggestions",
    "target_text",
]
