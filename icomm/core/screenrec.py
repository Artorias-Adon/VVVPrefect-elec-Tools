"""录屏用的 MJPEG-AVI 写入器（纯标准库，不依赖 ffmpeg）。

为什么是 AVI + MJPEG：

* 现场机器不一定装了 ffmpeg，而 Python 侧没有现成的视频编码器；
* Qt 自带 JPEG 编码器（打包里就有 ``qjpeg.dll``），一帧压成一张 JPEG 很便宜；
* MJPEG 塞进 AVI 容器只是拼几个 RIFF 块，几十行代码能写完，
  Windows 自带播放器 / VLC / 剪辑软件都认，发给同事不用额外装解码器。

用法::

    writer = AviMjpegWriter("a.avi", 1280, 720, fps=10)
    writer.add_frame(jpeg_bytes)   # 尺寸必须和构造时一致
    writer.close()

帧是**边录边写**的（写文件时留出头部位，收尾再回填总帧数/长度），
所以录 10 分钟也不会把内存吃满。
"""

from __future__ import annotations

import os
import struct

#: AVI 的帧块标识（压缩视频帧）
FRAME_CHUNK = b"00dc"
#: idx1 里的"这是关键帧"标志（MJPEG 每帧都是关键帧）
AVIIF_KEYFRAME = 0x00000010


def _u32(value: int) -> bytes:
    return struct.pack("<I", int(value) & 0xFFFFFFFF)


def even(value: int) -> int:
    """JPEG 要求宽高是偶数（奇数时编码器会自己裁，干脆先对齐）。"""
    return max(2, int(value) // 2 * 2)


class AviMjpegWriter:
    """把一串 JPEG 帧写成 MJPEG 编码的 AVI 文件。"""

    def __init__(self, path: str, width: int, height: int, fps: float = 10.0) -> None:
        self.path = os.path.abspath(path)
        self.width = even(width)
        self.height = even(height)
        self.fps = max(1.0, float(fps))
        self._frames = 0
        self._bytes = 0
        self._index: list[tuple[int, int]] = []
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._fh = open(self.path, "wb+")
        self._write_header()

    # -------------------------------------------------- 头部

    def _write_header(self) -> None:
        fh = self._fh
        fh.write(b"RIFF")
        self._riff_size_pos = fh.tell()
        fh.write(_u32(0))
        fh.write(b"AVI ")

        # LIST hdrl
        fh.write(b"LIST")
        self._hdrl_size_pos = fh.tell()
        fh.write(_u32(0))
        self._hdrl_data = fh.tell()
        fh.write(b"hdrl")

        # avih：14 个 DWORD
        fh.write(b"avih")
        fh.write(_u32(56))
        self._avih_pos = fh.tell()
        fh.write(
            struct.pack(
                "<14I",
                int(1_000_000 / self.fps),  # 每帧多少微秒
                0,                          # 最大字节率（收尾回填）
                0,                          # 对齐粒度
                AVIIF_KEYFRAME,             # 标志：带索引
                0,                          # 总帧数（收尾回填）
                0,                          # 初始帧数
                1,                          # 流个数
                0,                          # 建议缓冲（收尾回填）
                self.width,
                self.height,
                0, 0, 0, 0,                 # 保留
            )
        )

        # LIST strl
        fh.write(b"LIST")
        self._strl_size_pos = fh.tell()
        fh.write(_u32(0))
        self._strl_data = fh.tell()
        fh.write(b"strl")

        # strh：视频流头（56 字节）
        fh.write(b"strh")
        fh.write(_u32(56))
        self._strh_pos = fh.tell()
        fh.write(
            struct.pack(
                "<4s4sIHH" + "I" * 8 + "hhhh",
                b"vids",
                b"MJPG",
                0,                     # dwFlags
                0,                     # wPriority
                0,                     # wLanguage
                0,                     # dwInitialFrames
                1,                     # dwScale
                int(round(self.fps)),  # dwRate：每秒多少帧
                0,                     # dwStart
                0,                     # dwLength（收尾回填）
                0,                     # dwSuggestedBufferSize（收尾回填）
                0xFFFFFFFF,            # dwQuality：默认
                0,                     # dwSampleSize
                0, 0, self.width, self.height,  # rcFrame
            )
        )

        # strf：BITMAPINFOHEADER（40 字节），压缩方式写成 MJPG
        fh.write(b"strf")
        fh.write(_u32(40))
        fh.write(
            struct.pack(
                "<IiiHHIIiiII",
                40,
                self.width,
                self.height,
                1,                       # biPlanes
                24,                      # biBitCount
                int.from_bytes(b"MJPG", "little"),
                self.width * self.height * 3,
                0, 0, 0, 0,
            )
        )

        # 收尾 strl / hdrl 的 LIST 尺寸
        strl_end = fh.tell()
        fh.seek(self._strl_size_pos)
        fh.write(_u32(strl_end - (self._strl_size_pos + 4)))
        fh.seek(strl_end)
        hdrl_end = fh.tell()
        fh.seek(self._hdrl_size_pos)
        fh.write(_u32(hdrl_end - (self._hdrl_size_pos + 4)))
        fh.seek(hdrl_end)

        # LIST movi
        fh.write(b"LIST")
        self._movi_size_pos = fh.tell()
        fh.write(_u32(0))
        self._movi_start = fh.tell()
        fh.write(b"movi")

    # -------------------------------------------------- 帧

    def add_frame(self, jpeg: bytes) -> None:
        """追加一帧 JPEG（尺寸必须与构造时一致）。"""
        if not jpeg:
            raise ValueError("空帧")
        fh = self._fh
        position = fh.tell()
        fh.write(FRAME_CHUNK)
        fh.write(_u32(len(jpeg)))
        fh.write(jpeg)
        if len(jpeg) % 2:
            fh.write(b"\0")
        # idx1 里的偏移以 'movi' 这四个字节的位置为基准（惯例：第一帧是 4）
        self._index.append((position - self._movi_start, len(jpeg)))
        self._frames += 1
        self._bytes += len(jpeg)

    # -------------------------------------------------- 收尾

    @property
    def frames(self) -> int:
        return self._frames

    @property
    def frame_bytes(self) -> int:
        return self._bytes

    @property
    def duration(self) -> float:
        return self._frames / self.fps if self.fps else 0.0

    def close(self) -> str:
        """写完索引与长度，关文件；返回录像路径。"""
        if self._fh.closed:
            return self.path
        fh = self._fh
        movi_end = fh.tell()

        # idx1：每条 16 字节（标识 / 标志 / 偏移 / 长度）
        fh.write(b"idx1")
        fh.write(_u32(16 * len(self._index)))
        for offset, size in self._index:
            fh.write(FRAME_CHUNK)
            fh.write(_u32(AVIIF_KEYFRAME))
            fh.write(_u32(offset))
            fh.write(_u32(size))
        file_end = fh.tell()

        max_frame = max((size for _offset, size in self._index), default=0)
        # 回填 movi / hdrl / RIFF 的长度
        fh.seek(self._movi_size_pos)
        fh.write(_u32(movi_end - self._movi_start))
        # 回填 avih 的总帧数 / 最大字节率 / 建议缓冲
        fh.seek(self._avih_pos + 4)
        fh.write(_u32(int(max_frame * self.fps)))
        fh.seek(self._avih_pos + 16)
        fh.write(_u32(self._frames))
        fh.seek(self._avih_pos + 28)
        fh.write(_u32(max_frame))
        # 回填 strh 的长度 / 建议缓冲
        fh.seek(self._strh_pos + 32)
        fh.write(_u32(self._frames))
        fh.seek(self._strh_pos + 36)
        fh.write(_u32(max_frame))
        # 回填 RIFF 总长度
        fh.seek(self._riff_size_pos)
        fh.write(_u32(file_end - 8))
        fh.flush()
        fh.close()
        return self.path


def probe_avi(path: str) -> dict:
    """读回 AVI 头部（测试/自检用）：帧数、宽高、帧率、块数量。"""

    import struct as _struct

    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] != b"RIFF" or data[8:12] != b"AVI ":
        raise ValueError("不是 RIFF/AVI 文件")
    riff_size = _struct.unpack_from("<I", data, 4)[0]
    info: dict = {"riff_size": riff_size, "file_size": len(data)}
    avih = data.find(b"avih")
    if avih < 0:
        raise ValueError("缺少 avih")
    fields = _struct.unpack_from("<14I", data, avih + 8)
    info["frames"] = fields[4]
    info["width"] = fields[8]
    info["height"] = fields[9]
    strh = data.find(b"strh")
    if strh < 0:
        raise ValueError("缺少 strh")
    rate = _struct.unpack_from("<I", data, strh + 8 + 24)[0]
    info["fps"] = rate
    info["chunks"] = data.count(FRAME_CHUNK)
    info["has_index"] = b"idx1" in data
    return info
