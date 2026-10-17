"""IEC 61850 驱动（MMS 客户端）。

链路只有 ``tcp``：61850 的 MMS 走 TCP（标准端口 102），**一条通道 = 一个 IED 关联**。
所以"多台装置"就建多条通道；同一台装置内部有多个逻辑设备（LD）时，
把它们当作同一条通道下的多台"设备"即可，点号前缀也就自然分开了。

点位怎么填：

* 「寄存器区」列 = **功能约束 FC**：``MX`` 测量值、``ST`` 状态信息、``CO`` 控制、
  ``SP`` 定值、``CF`` 配置、``DC`` 描述……（点表里的 ``MEAS$MX$…`` 写法自带 FC 时，
  这一列留空也行，驱动会从路径里取）；
* 「协议路径」列 = **对象引用**：``MEAS/MMXU1.Hz.mag.f``；MMS 的分段写法
  （``MEAS$MX$MMXU1$Hz$mag$f``）也认。
* **线上逻辑设备名是"IED 名 + LD 名"**：装置 VVPSIM 的 MEAS 段在 MMS 上叫 ``VVPSIMMEAS``。
  所以通道参数里有一项「IED 名」，填上它以后点表就按 ICD 文件里那样写
  ``MEAS/MMXU1.Hz.mag.f``，驱动自己拼成线上引用；留空则要写全
  ``VVPSIMMEAS/MMXU1.Hz.mag.f``。
* 缩放/偏移照旧可用（装置给的是浮点工程量，默认 scale=1 就是原值）。

实现要点：

* 这个绑定的客户端是 **async** 的，而驱动的接口是同步的（引擎按通道起线程调），
  所以驱动自己起一条**事件循环线程**，所有 MMS 调用都投递到那个循环里执行，
  两边用 ``run_coroutine_threadsafe`` 对接 —— 调用方阻塞等结果，超时可控；
* 批量读走 MMS 的 ``read_multiple``：一次请求读一批对象，而不是一个点位一次往返；
* **报告订阅**：装上 handler 后由循环里的任务定期 ``poll_reports``，
  报告里的值用 :meth:`ChannelDriver.emit_values` 主动交给引擎 ——
  设备推上来的值不走"问一次答一次"，引擎那边也按同一套后处理（归档、报警、刷新）走；
* 单点位失败不影响别的点位（``strict=False``，逐项带回失败原因）。
"""

from __future__ import annotations

import asyncio
import re
import threading
from typing import Any, Iterable

from ..models import DeviceDef, PointDef
from .base import (
    ChannelDriver,
    DriverError,
    DriverNotOpen,
    OptionField,
    PointResult,
    register_driver,
)

try:
    from iec61850 import FC, IedConnection

    _IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - 取决于运行环境
    IedConnection = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)

    class _FcFallback(dict):
        """没有 libiec61850 绑定时顶替 ``FC``：把 FC 名字原样映射回自己。

        纯逻辑（引用解析、批量分块、写值前的校验）不该因为缺库就跑不了 ——
        真正连装置时 :meth:`Iec61850Driver.open` 会据 ``_IMPORT_ERROR``
        明确告诉用户"缺依赖"，而不是在执行到一半时抛 ``TypeError``。
        """

        def __getitem__(self, key: str) -> str:
            return key

    FC = _FcFallback()  # type: ignore[assignment]

#: MMS 客户端端口
DEFAULT_PORT = 102
#: 一次 MMS Read 最多带多少个对象（太大的请求有的装置会直接拒）
BATCH_SIZE = 40
#: 一次 MMS Read 的默认超时（毫秒）
REQUEST_TIMEOUT_MS = 4000

#: 「寄存器区」列的可选项：就是 61850 的功能约束，常用的排在前面
FC_CHOICES: list[tuple[str, str]] = [
    ("MX", "MX 测量值"),
    ("ST", "ST 状态信息"),
    ("CO", "CO 控制"),
    ("SP", "SP 定值"),
    ("CF", "CF 配置"),
    ("DC", "DC 描述"),
    ("SG", "SG 定值组"),
    ("SE", "SE 定值组编辑"),
    ("SV", "SV 替代值"),
    ("EX", "EX 扩展"),
    ("RP", "RP 无缓冲报告"),
    ("BR", "BR 有缓冲报告"),
    ("LG", "LG 日志"),
]
FC_NAMES = {name for name, _label in FC_CHOICES}
#: 不写 FC 时的兜底（测量值最常见）。写错 FC 会读不到，报错里会提示。
DEFAULT_FC = "MX"


def normalise_reference(text: str) -> tuple[str, str]:
    """把点表里写的引用整理成 ``(MMS 引用, 自带的 FC 或空串)``。

    认两种写法：

    * ``MEAS/MMXU1.Hz.mag.f`` —— 标准对象引用，FC 得另外说；
    * ``MEAS$MX$MMXU1$Hz$mag$f`` —— 装置导出/抓包常见的写法，第 2 段就是 FC。

    返回的引用统一成 ``LD/LN.DO[.DA]``；识别不了就抛 :class:`DriverError`，
    错误信息里带上正确写法 —— 现场填错这一列的概率很高，报错要能自己说明白。
    """
    raw = (text or "").strip()
    if not raw:
        raise DriverError(
            "协议路径没填：IEC 61850 点位要写对象引用，例如 MEAS/MMXU1.Hz.mag.f"
        )
    if "$" in raw:
        parts = [part for part in raw.split("$") if part != ""]
        if len(parts) >= 3 and parts[1] in FC_NAMES:
            fc = parts[1]
            rest = parts[:1] + parts[2:]
        else:
            fc = ""
            rest = parts
        if len(rest) < 2:
            raise DriverError(f"协议路径「{text}」看不懂，至少要 LD 和 LN，例如 MEAS/MMXU1.Hz.mag.f")
        reference = f"{rest[0]}/{'.'.join(rest[1:])}"
        return reference, fc
    if "/" not in raw:
        raise DriverError(
            f"协议路径「{text}」缺逻辑设备名，要写成 LD/LN.DO[.DA]，例如 MEAS/MMXU1.Hz.mag.f"
        )
    return raw.lstrip("/"), ""


def reference_key(reference: str) -> str:
    """引用的比对键。

    报告里的写法可能和点表不完全一致（分段写法还带 FC 段），所以先按同一套规则
    整理成标准引用，再统一成小写、点分形式来比。
    """
    text = (reference or "").strip()
    try:
        text, _fc = normalise_reference(text)
    except DriverError:
        text = text.lstrip("/")
    return text.replace("$", "/").replace(".", "/").lower()


def _chunks(items: list, size: int) -> Iterable[list]:
    size = max(1, int(size))
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _failure_reason(value) -> str:
    """批量读里"这一项失败了"的判定。

    ``strict=False`` 时失败项不是普通值，而是库自己的失败对象（``DataAccessFailure``，
    带 ``error`` / ``code``）。这里用鸭子类型判，不去 import 那个类 ——
    库换版本、类名变了也不影响。
    """
    error = getattr(value, "error", None)
    if isinstance(error, str) and error:
        return error
    if isinstance(value, dict):
        marker = value.get("error")
        if isinstance(marker, str) and marker:
            return marker
    return ""


@register_driver
class Iec61850Driver(ChannelDriver):
    """IEC 61850 MMS 客户端驱动。"""

    driver_name = "iec61850"
    link_types = ("tcp",)
    description = "IEC 61850（MMS over TCP：读写数据对象 + 报告订阅）"

    @classmethod
    def point_areas(cls) -> list[tuple[str, str]]:
        return list(FC_CHOICES)

    @classmethod
    def point_area_hint(cls) -> str:
        return (
            "「寄存器区」填功能约束 FC（MX 测量 / ST 状态 / CO 控制 / SP 定值 …），"
            "「协议路径」填对象引用（如 MEAS/MMXU1.Hz.mag.f）"
        )

    @classmethod
    def option_schema(cls, link: str) -> list[OptionField]:
        return [
            OptionField(
                key="host",
                label="IED 地址",
                type="str",
                default="192.168.1.10",
                hint="装置的 IP 或主机名。一条通道对应一台装置（一个 MMS 关联）",
            ),
            OptionField(
                key="ied_name",
                label="IED 名",
                type="str",
                default="",
                hint="IED 名（如 VVPSIM）。填上它，点表里就能按 ICD 写法只写 MEAS/MMXU1.Hz.mag.f",
            ),
            OptionField(
                key="port",
                label="端口",
                type="int",
                default=DEFAULT_PORT,
                minimum=1,
                maximum=65535,
                hint="MMS 标准端口 102",
            ),
            OptionField(
                key="timeout_ms",
                label="连接超时(ms)",
                type="int",
                default=5000,
                minimum=500,
                maximum=60000,
            ),
            OptionField(
                key="request_timeout_ms",
                label="单次请求超时(ms)",
                type="int",
                default=REQUEST_TIMEOUT_MS,
                minimum=200,
                maximum=60000,
                hint="一次批量读 / 写值的等待上限",
            ),
            OptionField(
                key="batch_size",
                label="批读对象数",
                type="int",
                default=BATCH_SIZE,
                minimum=1,
                maximum=200,
                hint="一次 MMS Read 带多少个对象。太大有的装置会拒，太小则往返次数多",
            ),
            OptionField(
                key="reports",
                label="订阅报告",
                type="bool",
                default=True,
                hint="按下面的报告控制块订阅，装置推上来的值直接进实时表（秒级刷新）",
            ),
            OptionField(
                key="rcb_refs",
                label="报告控制块",
                type="str",
                default="",
                hint="多个用逗号分隔，如 MEAS/LLN0.RP1、PROT/LLN0.BR1；留空就不订阅",
            ),
            OptionField(
                key="report_poll_ms",
                label="报告轮询(ms)",
                type="int",
                default=200,
                minimum=50,
                maximum=2000,
                hint="多久去取一次报告队列（值本身是装置推上来的，这里只是收件频率）",
            ),
        ]

    # ------------------------------------------------------------ 生命周期

    def __init__(self, channel) -> None:
        super().__init__(channel)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._conn = None
        self._report_future = None
        self._running = False
        #: 归一化引用 -> 点号（报告里的对象要映射回点位）
        self._ref_to_pid: dict[str, str] = {}
        #: 报告里出现过、点表里却没有的对象（只提示一次，免得刷屏）
        self._warned_refs: set[str] = set()
        self._lock = threading.Lock()

    def set_project(self, project) -> None:
        """拿到整个工程：报告里的对象要靠它映射回点位。

        报告是装置按数据集推上来的，可能包含点表里没在轮询的对象，
        所以不能只靠"读过哪些点位"来建映射表。
        """
        super().set_project(project)
        self._rebuild_ref_map()

    @property
    def _options(self) -> dict:
        return dict(getattr(self.channel, "options", {}) or {})

    @property
    def address(self) -> str:
        options = self._options
        host = str(options.get("host") or "127.0.0.1").strip()
        port = int(options.get("port") or DEFAULT_PORT)
        return f"{host}:{port}"

    def describe(self) -> str:
        return f"iec61850@{self.address}"

    def open(self) -> None:
        if self.is_open:
            return
        if IedConnection is None:
            raise DriverError(
                "未安装 iec61850 库，无法使用 61850 驱动"
                + (f"（{_IMPORT_ERROR}）" if _IMPORT_ERROR else "")
                + "。安装：pip install iec61850"
            )
        options = self._options
        self._start_loop()
        timeout_ms = int(options.get("timeout_ms") or 5000)
        request_ms = int(options.get("request_timeout_ms") or REQUEST_TIMEOUT_MS)
        try:
            self._conn = self._call(
                lambda: IedConnection.connect(
                    self.address, timeout_ms=timeout_ms, request_timeout_ms=request_ms
                ),
                timeout=timeout_ms / 1000.0 + 1.0,
            )
        except Exception as exc:
            self._stop_loop()
            raise DriverError(f"连接 {self.address} 失败：{exc}") from exc
        self._rebuild_ref_map()
        self._install_reports()
        self.emit_log("info", f"{self.describe()} 已连接")

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                self._call(lambda: conn.disconnect(), timeout=3.0)
            except Exception:
                try:
                    self._call(lambda: conn.abort(), timeout=2.0)
                except Exception:
                    pass
        self._stop_loop()

    @property
    def is_open(self) -> bool:
        conn = self._conn
        return bool(conn is not None and getattr(conn, "is_connected", False))

    # ------------------------------------------------------------ 事件循环线程

    def _start_loop(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, name=f"icomm-61850-{self.channel.name}", daemon=True
        )
        self._thread.start()

    def _run_loop(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _stop_loop(self) -> None:
        self._running = False
        loop, thread = self._loop, self._thread
        self._loop = None
        self._thread = None
        self._report_future = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=3.0)
        if loop is not None:
            try:
                loop.close()
            except Exception:
                pass

    def _call(self, factory, timeout: float):
        """把协程投到驱动自己的循环里执行，并同步等结果。

        ``factory`` 必须是个**可调用对象**（返回协程），不能直接传协程 ——
        直接传协程的话在调用方线程就被创建了，那个线程没有事件循环。
        """
        loop = self._loop
        if loop is None:
            raise DriverNotOpen("61850 客户端事件循环没起来")
        future = asyncio.run_coroutine_threadsafe(factory(), loop)
        return future.result(timeout=timeout)

    def _request_timeout(self) -> float:
        return int(self._options.get("request_timeout_ms") or REQUEST_TIMEOUT_MS) / 1000.0 + 1.0

    # ------------------------------------------------------------ 报告订阅

    def _install_reports(self) -> None:
        options = self._options
        if not bool(options.get("reports", True)):
            return
        raw = str(options.get("rcb_refs") or "")
        refs = [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]
        if not refs or self._conn is None:
            return
        conn = self._conn
        installed = 0
        for ref in refs:
            try:
                self._call(
                    lambda ref=ref: conn.install_report_handler(ref, self._on_report),
                    timeout=self._request_timeout(),
                )
            except Exception as exc:
                self.emit_log("warning", f"订阅报告 {ref} 失败：{exc}")
                continue
            installed += 1
        if not installed:
            return
        # 报告是装置推上来的：这里只负责定期把队列取空
        self._report_future = asyncio.run_coroutine_threadsafe(self._report_loop(), self._loop)
        self.emit_log("info", f"已订阅 {installed} 个报告控制块：{'、'.join(refs)}")

    async def _report_loop(self) -> None:
        interval = max(0.05, int(self._options.get("report_poll_ms") or 200) / 1000.0)
        while self._running:
            conn = self._conn
            if conn is None:
                await asyncio.sleep(interval)
                continue
            try:
                await conn.poll_reports(int(interval * 1000))
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - 报告断了不该把驱动弄死
                self.emit_log("warning", f"取报告出错：{exc}")
                await asyncio.sleep(0.5)

    def _on_report(self, report) -> None:
        """收到一份报告：把里面的对象翻成点号，交回引擎。

        这个回调在驱动的循环线程里被调用，所以只做"翻译 + 上送"，
        归档/报警/界面刷新都由引擎在通道线程里统一处理。
        """
        with self._lock:
            mapping = dict(self._ref_to_pid)
        values: dict[str, Any] = {}
        for entry in getattr(report, "entries", None) or []:
            ref = getattr(entry, "ref", None)
            if not ref:
                continue
            pid = mapping.get(reference_key(str(ref)))
            if pid is None:
                self._note_unmapped(str(ref))
                continue
            values[pid] = getattr(entry, "value", None)
        if values:
            self.emit_values(values)

    def _note_unmapped(self, reference: str) -> None:
        key = reference_key(reference)
        with self._lock:
            if key in self._warned_refs:
                return
            self._warned_refs.add(key)
        self.emit_log(
            "info",
            f"报告里有对象 {reference}，点表里没有对应点位（"
            "在点表里加一条协议路径相同的点位就能显示它）",
        )

    # ------------------------------------------------------------ 引用与映射

    def _resolve(self, device: DeviceDef, point: PointDef) -> tuple[str, str]:
        """算出点位的 ``(MMS 引用, FC)``。

        线上逻辑设备名是"IED 名 + LD 名"，所以点表按 ICD 写法填的时候这里补上 IED 名 ——
        现场点表都是照 ICD 文件抄的，不该逼着人手工拼成线上名字。
        """
        reference, path_fc = normalise_reference(point.path)
        raw_fc = (point.register or "").strip().upper()
        fc = raw_fc if raw_fc in FC_NAMES else (path_fc or DEFAULT_FC)
        ied = str(self._options.get("ied_name") or "").strip()
        if ied and "/" in reference:
            ld, _, rest = reference.partition("/")
            if not ld.upper().startswith(ied.upper()):
                reference = f"{ied}{ld}/{rest}"
        return reference, fc

    def _rebuild_ref_map(self) -> None:
        """按工程里的点位建"引用 -> 点号"映射（报告回填用）。"""
        project = self._project
        mapping: dict[str, str] = {}
        if project is not None:
            for device, point in project.iter_points():
                if device.channel and device.channel != self.channel.name:
                    continue
                try:
                    reference, _fc = self._resolve(device, point)
                except DriverError:
                    continue
                mapping[reference_key(reference)] = f"{device.name}.{point.name}"
        with self._lock:
            self._ref_to_pid = mapping

    # ------------------------------------------------------------ 读写

    def read_points(self, device: DeviceDef, points: Iterable[PointDef]) -> dict[str, PointResult]:
        if not self.is_open:
            raise DriverNotOpen(f"{self.describe()} 未连接")
        out: dict[str, PointResult] = {}
        targets: list[tuple[str, str]] = []
        meta: dict[str, PointDef] = {}
        for point in points:
            try:
                reference, fc = self._resolve(device, point)
            except DriverError as exc:
                out[point.name] = PointResult(point.name, None, False, str(exc))
                continue
            if point.is_array:
                # 数据集整段读走 get_data_set_values，不在批量读里混
                out[point.name] = self._read_array(reference)
                continue
            # MMS 客户端要的是 FC 枚举，不是字符串 —— 传字符串会在库里炸掉
            targets.append((reference, FC[fc]))
            meta[reference] = point

        size = int(self._options.get("batch_size") or BATCH_SIZE)
        conn = self._conn
        for chunk in _chunks(targets, size):
            try:
                values = self._call(
                    lambda chunk=chunk: conn.read_multiple(list(chunk), strict=False),
                    timeout=self._request_timeout(),
                )
            except Exception as exc:
                for reference, _fc in chunk:
                    point = meta[reference]
                    out[point.name] = PointResult(point.name, None, False, f"读取失败：{exc}")
                continue
            for (reference, fc), value in zip(chunk, values):
                point = meta[reference]
                out[point.name] = self._point_result(point, reference, fc, value)
        return out

    def _point_result(self, point: PointDef, reference: str, fc: str, value) -> PointResult:
        reason = _failure_reason(value)
        if reason:
            # 读不到十有八九是 FC 写错（对象在 ST 你按 MX 读），提示直接写上，省一轮猜
            hint = f"（当前按 FC={fc} 读；若装置里这个对象不属于该 FC，请改「寄存器区」列）"
            return PointResult(point.name, None, False, f"装置返回 {reason}{hint}")
        if value is None:
            return PointResult(point.name, None, False, "装置没给值")
        try:
            return PointResult(point.name, self._engineer(point, value), True, "")
        except (TypeError, ValueError) as exc:
            return PointResult(point.name, None, False, f"值 {value!r} 无法换算：{exc}")

    def _read_array(self, reference: str) -> PointResult:
        """数组点位：按数据集读回整段。

        61850 里成段的数据一般做成 DataSet，这里直接读它；
        不是数据集就报错说明 —— 猜一个范围去逐个读反而更慢更乱。
        """
        conn = self._conn
        try:
            values = self._call(
                lambda: conn.get_data_set_values(reference, strict=False),
                timeout=self._request_timeout(),
            )
        except Exception as exc:
            return PointResult(reference, None, False, f"读数据集失败：{exc}")
        numbers = [float(v) for v in values if isinstance(v, (int, float))]
        return PointResult(reference, numbers, True, "")

    @staticmethod
    def _from_bytes(value):
        """位串 / 八位组串（开关位置 Dbpos、品质 q 之类）转成整数。

        MMS 的位串是"高位对齐"装在字节里的：Dbpos 的 0/1/2/3 会回 0x00/0x40/0x80/0xC0。
        所以一个字节里只有高 2 位非 0 时右移 6 位 —— 开关位置显示的就是 0~3，
        而不是让现场对着 0/64/128/192 发懵。普通八位组串（低位有值）原样转整数。
        """
        if not isinstance(value, (bytes, bytearray)):
            return value
        raw = bytes(value)
        if len(raw) == 1 and raw[0] & 0x3F == 0:
            return raw[0] >> 6
        return int.from_bytes(raw, "big")

    @staticmethod
    def _engineer(point: PointDef, value):
        """装置给的是工程量，默认原样；用户填了缩放/偏移就照填的算。"""
        value = Iec61850Driver._from_bytes(value)
        if isinstance(value, bool):
            return value
        scale = float(point.scale or 1.0)
        offset = float(point.offset or 0.0)
        if scale == 1.0 and offset == 0.0:
            return value
        try:
            return float(value) * scale + offset
        except (TypeError, ValueError):
            return value

    def write_point(self, device: DeviceDef, point: PointDef, value: Any) -> None:
        if not self.is_open:
            raise DriverNotOpen(f"{self.describe()} 未连接")
        reference, fc = self._resolve(device, point)
        converted = self._coerce(point, value)
        conn = self._conn
        try:
            self._call(
                lambda: conn.write(reference, FC[fc], converted),
                timeout=self._request_timeout(),
            )
        except Exception as exc:
            raise DriverError(f"写 {reference}（{fc}）失败：{exc}") from exc

    @staticmethod
    def _coerce(point: PointDef, value: Any):
        """按点位的数据类型把值转成装置认的类型。"""
        kind = (point.data_type or "").lower()
        if kind == "bool" or isinstance(value, bool):
            return bool(value)
        if kind.startswith("uint") or kind.startswith("int"):
            return int(round(float(value)))
        if kind.startswith("float") or kind == "":
            return float(value)
        return value

    # ------------------------------------------------------------ 浏览（给工具用）

    def browse_server(self) -> list[str]:
        """服务器里有哪些逻辑设备。"""
        if not self.is_open:
            raise DriverNotOpen(f"{self.describe()} 未连接")
        conn = self._conn
        return list(self._call(lambda: conn.get_server_directory(), timeout=self._request_timeout()))

    def browse_logical_device(self, ld: str) -> list[str]:
        """某个逻辑设备里有哪些逻辑节点（返回 LN 名，如 ``LLN0``、``MMXU1``）。"""
        if not self.is_open:
            raise DriverNotOpen(f"{self.describe()} 未连接")
        conn = self._conn
        return list(
            self._call(
                lambda: conn.get_logical_device_directory(ld), timeout=self._request_timeout()
            )
        )

    def browse_data_objects(self, ld: str, ln: str) -> list[str]:
        """某个逻辑节点下有哪些数据对象。

        LN 类名要单独给（``MMXU1`` 的类名是 ``MMXU``），这里从名字里推：
        去掉末尾数字即可，``LLN0`` 本身就是类名。这个形式是实测出来的，
        只给 ``LD/LN`` 不带类名会被直接拒。
        """
        if not self.is_open:
            raise DriverNotOpen(f"{self.describe()} 未连接")
        conn = self._conn
        ln_class = re.sub(r"\d+$", "", ln) or ln
        return list(
            self._call(
                lambda: conn.get_logical_node_directory(f"{ld}/{ln}", ln_class),
                timeout=self._request_timeout(),
            )
        )

    def browse_attributes(self, ld: str, ln: str, do: str) -> list[str]:
        """某个数据对象下有哪些属性（``LD/LN.DO`` 形式，实测可用）。"""
        if not self.is_open:
            raise DriverNotOpen(f"{self.describe()} 未连接")
        conn = self._conn
        return list(
            self._call(
                lambda: conn.get_data_directory(f"{ld}/{ln}.{do}"),
                timeout=self._request_timeout(),
            )
        )
