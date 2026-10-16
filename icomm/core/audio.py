"""录音（Windows 自带 winmm，不加任何第三方依赖）。

现场的用法通常是"出故障时把当时的声音也留一份"——继电器动作声、放电声、
风机异响，事后翻录音比翻曲线直观得多。所以这里和故障录波一样：
由变量触发自动开录，录够时长自动停。

实现方式：直接用 ctypes 调 ``winmm`` 的 waveIn* 系列 API（WAVE_FORMAT_PCM 16 位），
**不引入任何新依赖**（这台机器上连 Pillow 都没有，能不加依赖就不加）。
数据在音频线程的回调里收集，由后台线程落盘，界面线程只负责显示；
所以录音期间调界面不会丢采样。没有录音设备（或不是 Windows）时
:func:`devices` 返回空、:class:`WinmmRecorder.open` 会给出明确原因，界面照常能开。
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

IS_WINDOWS = sys.platform.startswith("win")

#: 默认采样参数（16kHz 单声道 16 位，语音/异响足够，1 分钟约 1.9 MB）
DEFAULT_RATE = 16_000
DEFAULT_CHANNELS = 1
BITS = 16

#: 一次录最多多久（秒），0 = 不限制
MAX_DURATION_S = 3600.0
#: 单个文件大小上限，超过就自己停（防止没人看着把磁盘写满）
MAX_BYTES = 512 * 1024 * 1024

# --------------------------------------------------------------- WAV 写盘


class WavWriter:
    """边录边写的 WAV（16 位 PCM）。

    先写 44 字节占位头，收尾时回填长度 —— 录音中断电、被杀进程时文件头虽然不对，
    但数据还在（用 Audacity 之类"导入原始数据"仍能救回来）。
    """

    HEADER_SIZE = 44

    def __init__(self, path: str, rate: int = DEFAULT_RATE, channels: int = DEFAULT_CHANNELS) -> None:
        self.path = path
        self.rate = int(rate)
        self.channels = int(channels)
        self.frames = 0
        self.bytes_written = 0
        self._fh = open(path, "wb")  # noqa: SIM115 - 生命周期由 close() 管
        self._fh.write(b"\x00" * self.HEADER_SIZE)
        self._closed = False

    def write(self, data: bytes) -> int:
        if self._closed or not data:
            return 0
        # 奇数长度会导致块对齐错乱，丢弃最后一个字节（16 位采样不该出现，保险起见）
        usable = len(data) - (len(data) % 2)
        if usable <= 0:
            return 0
        self._fh.write(data[:usable])
        self.bytes_written += usable
        block = max(1, (BITS // 8) * self.channels)
        self.frames += usable // block
        return usable

    @property
    def duration(self) -> float:
        return self.frames / self.rate if self.rate else 0.0

    @property
    def size(self) -> int:
        return self.HEADER_SIZE + self.bytes_written

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._fh.seek(0)
            self._fh.write(self.header())
            self._fh.flush()
        finally:
            self._fh.close()

    def header(self) -> bytes:
        """标准 44 字节 RIFF 头（按已写入的数据算长度）。"""
        byte_rate = self.rate * self.channels * (BITS // 8)
        block_align = self.channels * (BITS // 8)
        return b"".join(
            (
                b"RIFF",
                struct.pack("<I", 36 + self.bytes_written),
                b"WAVEfmt ",
                struct.pack("<IHHIIHH", 16, 1, self.channels, self.rate, byte_rate, block_align, BITS),
                b"data",
                struct.pack("<I", self.bytes_written),
            )
        )


# --------------------------------------------------------------- 设备


@dataclass
class AudioDevice:
    index: int
    name: str
    channels: int


def devices() -> list[AudioDevice]:
    """系统里的录音设备（拿不到就返回空列表，不抛异常）。"""
    if not IS_WINDOWS:
        return []
    try:
        winmm = ctypes.WinDLL("winmm")  # type: ignore[attr-defined]
    except OSError:
        return []
    count = winmm.waveInGetNumDevs()
    found: list[AudioDevice] = []
    for index in range(count):
        caps = _WAVEINCAPS()
        result = winmm.waveInGetDevCapsW(
            ctypes.c_void_p(index), ctypes.byref(caps), ctypes.sizeof(caps)
        )
        if result != 0:
            continue
        found.append(
            AudioDevice(index=index, name=caps.szPname or f"录音设备 {index}", channels=caps.wChannels)
        )
    return found


class _WAVEINCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", ctypes.c_ushort),
        ("wPid", ctypes.c_ushort),
        ("vDriverVersion", ctypes.c_uint),
        ("szPname", ctypes.c_wchar * 32),
        ("dwFormats", ctypes.c_uint),
        ("wChannels", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
    ]


class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_uint),
        ("nAvgBytesPerSec", ctypes.c_uint),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class _WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", ctypes.c_uint),
        ("dwBytesRecorded", ctypes.c_uint),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint),
        ("dwLoops", ctypes.c_uint),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]


WIM_OPEN = 0x3BE
WIM_CLOSE = 0x3BF
WIM_DATA = 0x3C0
CALLBACK_FUNCTION = 0x00030000
MMSYSERR_NOERROR = 0
#: WAVEHDR.dwFlags 位：这块缓冲区已经填满了 / 已准备
WHDR_DONE = 0x01
WHDR_PREPARED = 0x02

# WINFUNCTYPE 只有 Windows 上才有；非 Windows 上保持 None，让本模块仍可被 import
_WAVEINPROC: Any = (
    ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
        None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    )
    if IS_WINDOWS
    else None
)

#: 不同缓冲区个数：4 个 0.25 秒的缓冲区，够容错又不至于延迟太大
BUFFER_COUNT = 4


class WinmmRecorder:
    """用 winmm 录音。

    ``open()`` 之后音频线程就开始往 :class:`WavWriter` 送数据；
    ``stop()`` 会停在写的这一侧并把 WAV 头回填好。
    """

    def __init__(self, path: str, rate: int = DEFAULT_RATE, channels: int = DEFAULT_CHANNELS,
                 device_index: int = 0, max_seconds: float = MAX_DURATION_S,
                 max_bytes: int = MAX_BYTES) -> None:
        self.writer = WavWriter(path, rate, channels)
        self.rate = int(rate)
        self.channels = int(channels)
        self.device_index = int(device_index)
        self.max_seconds = float(max_seconds or 0.0)
        self.max_bytes = int(max_bytes)
        self.error = ""
        self.overrun = 0

        self._winmm: Any = None
        self._handle = ctypes.c_void_p()
        self._headers: list[_WAVEHDR] = []
        self._buffers: list[Any] = []
        self._callback: Any = None
        self._lock = threading.Lock()
        self._pending: list[bytes] = []
        self._stop = threading.Event()
        self._closing = False
        self._drain_thread: threading.Thread | None = None
        self.started_at = 0.0

    # -------------------------------------------------- 设备

    @staticmethod
    def available() -> tuple[bool, str]:
        """能不能录，不能的话给出原因（界面拿它显示提示）。"""
        if not IS_WINDOWS:
            return False, "录音功能目前只支持 Windows（用的是系统自带 winmm）"
        try:
            ctypes.WinDLL("winmm")  # type: ignore[attr-defined]
        except OSError as exc:
            return False, f"加载 winmm 失败：{exc}"
        if not devices():
            return False, "系统里没有找到录音设备（插上麦克风/声卡输入再试）"
        return True, ""

    # -------------------------------------------------- 开关

    def open(self) -> bool:
        ok, reason = self.available()
        if not ok:
            self.error = reason
            self.writer.close()
            return False
        self._winmm = ctypes.WinDLL("winmm")  # type: ignore[attr-defined]

        fmt = _WAVEFORMATEX(
            wFormatTag=1,
            nChannels=self.channels,
            nSamplesPerSec=self.rate,
            nAvgBytesPerSec=self.rate * self.channels * (BITS // 8),
            nBlockAlign=self.channels * (BITS // 8),
            wBitsPerSample=BITS,
            cbSize=0,
        )
        self._callback = _WAVEINPROC(self._on_audio_event)
        err = self._winmm.waveInOpen(
            ctypes.byref(self._handle),
            ctypes.c_void_p(self.device_index),
            ctypes.byref(fmt),
            # CALLBACK_FUNCTION 时 dwCallback 要传回调函数地址（不是 0）
            ctypes.cast(self._callback, ctypes.c_void_p),
            ctypes.c_void_p(0),
            CALLBACK_FUNCTION,
        )
        if err != MMSYSERR_NOERROR:
            self.error = f"打开录音设备失败（waveInOpen 返回 {err}）"
            self.writer.close()
            return False

        if not self._queue_buffers():
            self.close()
            self.error = self.error or "给录音设备分配缓冲区失败"
            return False

        err = self._winmm.waveInStart(self._handle)
        if err != MMSYSERR_NOERROR:
            self.error = f"启动录音失败（waveInStart 返回 {err}）"
            self.close()
            return False

        self.started_at = time.time()
        self._stop.clear()
        self._drain_thread = threading.Thread(
            target=self._drain_loop, name="icomm-audio", daemon=True
        )
        self._drain_thread.start()
        return True

    def _queue_buffers(self) -> bool:
        block = max(1, self.rate * self.channels * (BITS // 8) // 4)  # 0.25 秒
        for _ in range(BUFFER_COUNT):
            buffer = ctypes.create_string_buffer(block)
            header = _WAVEHDR(
                lpData=ctypes.cast(buffer, ctypes.c_void_p),
                dwBufferLength=block,
                dwBytesRecorded=0,
                dwFlags=0,
            )
            err = self._winmm.waveInPrepareHeader(
                self._handle, ctypes.byref(header), ctypes.sizeof(header)
            )
            if err != MMSYSERR_NOERROR:
                self.error = f"waveInPrepareHeader 失败（{err}）"
                return False
            err = self._winmm.waveInAddBuffer(
                self._handle, ctypes.byref(header), ctypes.sizeof(header)
            )
            if err != MMSYSERR_NOERROR:
                self.error = f"waveInAddBuffer 失败（{err}）"
                return False
            self._headers.append(header)
            self._buffers.append(buffer)
        return True

    def _on_audio_event(self, _handle, message, _instance, param1, _param2) -> None:
        """音频线程回调：攒满一块就抄出来并把缓冲还回去。

        这里只做"拷贝 + 归还"，落盘交给后台线程 —— 在音频线程里写文件
        一旦磁盘卡顿就会丢采样。
        """
        if message != WIM_DATA or self._closing:
            return
        try:
            header = ctypes.cast(param1, ctypes.POINTER(_WAVEHDR)).contents
            if header.dwBytesRecorded:
                data = ctypes.string_at(header.lpData, header.dwBytesRecorded)
                with self._lock:
                    self._pending.append(data)
            # 只清 WHDR_DONE，**不能**把 dwFlags 整个清零 ——
            # WHDR_PREPARED 是驱动自己的记账，抹掉之后再 waveInAddBuffer
            # 会返回 WAVERR_UNPREPARED(34)，录音就卡在第一批缓冲区之后。
            header.dwBytesRecorded = 0
            header.dwFlags &= ~WHDR_DONE
            if not self._closing and self._handle:
                err = self._winmm.waveInAddBuffer(
                    self._handle, ctypes.byref(header), ctypes.sizeof(header)
                )
                if err != MMSYSERR_NOERROR:
                    self.overrun += 1
                    self.error = self.error or f"缓冲区回收失败（waveInAddBuffer 返回 {err}）"
        except Exception:  # noqa: BLE001 - 音频线程里绝不能把异常抛出去
            self.overrun += 1

    def _drain_loop(self) -> None:
        while not self._stop.is_set():
            self._drain()
            time.sleep(0.05)
        self._drain()

    def _drain(self) -> int:
        with self._lock:
            chunks, self._pending = self._pending, []
        written = 0
        for chunk in chunks:
            written += self.writer.write(chunk)
        return written

    # -------------------------------------------------- 状态

    @property
    def duration(self) -> float:
        return self.writer.duration

    @property
    def size(self) -> int:
        return self.writer.size

    def should_stop(self) -> str:
        """到达时长/体积上限就该自己停了，返回原因（没到返回空串）。"""
        if self.max_seconds and self.duration >= self.max_seconds:
            return f"到达设定时长（{self.max_seconds:g} 秒）"
        if self.max_bytes and self.size >= self.max_bytes:
            return f"到达文件大小上限（{self.max_bytes // 1024 // 1024} MB）"
        return ""

    def close(self) -> None:
        """停止录音、回填 WAV 头、释放设备。可重复调用。"""
        if self._closing:
            return
        self._closing = True
        try:
            if self._winmm is not None and self._handle:
                self._winmm.waveInStop(self._handle)
                self._winmm.waveInReset(self._handle)
            self._stop.set()
            thread = self._drain_thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
            self._drain_thread = None
            self._drain()
            if self._winmm is not None and self._handle:
                for header in self._headers:
                    self._winmm.waveInUnprepareHeader(
                        self._handle, ctypes.byref(header), ctypes.sizeof(header)
                    )
                self._winmm.waveInClose(self._handle)
        except Exception as exc:  # noqa: BLE001 - 关设备失败也不该把程序带崩
            self.overrun += 1
            self.error = self.error or f"关闭录音设备时出错：{exc}"
        finally:
            self._headers.clear()
            self._buffers.clear()
            self._handle = ctypes.c_void_p()
            self.writer.close()


# --------------------------------------------------------------- 空闲实现


class NullRecorder:
    """录不了时的替身：接口一样，但什么都不写，只报告原因。

    界面和测试都用它，这样"没有录音设备"也不会变成一堆分支判断。
    """

    def __init__(self, path: str, rate: int = DEFAULT_RATE, channels: int = DEFAULT_CHANNELS,
                 device_index: int = 0, max_seconds: float = 0.0,
                 error: str = "没有可用的录音设备") -> None:
        self.path = path
        self.writer = None
        self.error = error
        self.duration = 0.0
        self.size = 0

    @staticmethod
    def available() -> tuple[bool, str]:
        return False, "没有可用的录音设备"

    def open(self) -> bool:
        return False

    def should_stop(self) -> str:
        return ""

    def close(self) -> None:
        return None


def make_recorder(path: str, rate: int = DEFAULT_RATE, channels: int = DEFAULT_CHANNELS,
                  device_index: int = 0, max_seconds: float = MAX_DURATION_S):
    """按当前平台/设备选一个录音实现（录不了就返回 :class:`NullRecorder`）。

    测试里可以传一个假类进来替换，所以界面不直接 new :class:`WinmmRecorder`。
    """
    ok, reason = WinmmRecorder.available()
    if not ok:
        return NullRecorder(path, rate, channels, device_index, max_seconds, error=reason)
    return WinmmRecorder(
        path=path,
        rate=rate,
        channels=channels,
        device_index=device_index,
        max_seconds=max_seconds,
    )


def default_dir(base_dir: str) -> str:
    return os.path.join(base_dir or ".", "recordings")
