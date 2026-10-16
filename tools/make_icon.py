# -*- coding: utf-8 -*-
"""由 assets/icon.svg 生成 iComm 的图标资产。

用法（工程根目录）::

    python tools/make_icon.py

产物（assets/）::

    icon_16.png ... icon_256.png   各尺寸 PNG（32 位带 alpha）
    icon.ico                       多尺寸 Windows 图标（内嵌 PNG 数据）
    icon_preview.png               放大对照图，用于人工检查小尺寸可辨认度

实现要点：

* 不依赖 Pillow。SVG -> PNG 用 ``PySide6.QtSvg.QSvgRenderer`` 直接按目标像素
  尺寸光栅化（矢量直出，比先渲染 256 再缩小清晰得多）。
* ``.ico`` 容器手写：6 字节文件头 + 每尺寸 16 字节目录项 + 顺序拼接的 PNG 原始
  数据。Vista 及以上支持 ICO 内嵌 PNG，故不需要 BMP/DIB 编码。
* 16 / 24 px 下细节会糊，故小尺寸渲染前先删掉 SVG 里 ``glow-layer``（发光描边）
  和 ``detail-layer``（网格肌理），只留底板与粗线条主体。
* 无显示器环境运行：``QT_QPA_PLATFORM=offscreen``。
"""

from __future__ import annotations

import os
import re
import struct
import sys
from pathlib import Path

# 必须在导入 QtGui / 创建 QGuiApplication 之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QLocale, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
)
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SVG_PATH = ASSETS / "icon.svg"
ICO_PATH = ASSETS / "icon.ico"
PREVIEW_PATH = ASSETS / "icon_preview.png"

#: ICO 里包含的全部尺寸
PNG_SIZES = (16, 24, 32, 48, 64, 128, 256)

#: 小于该尺寸时删除发光层与肌理层
SMALL_SIZE_LIMIT = 32

#: 小尺寸下需要剥离的图层（glow 会糊成灰雾，网格会糊成噪点）
STRIPPED_LAYERS_SMALL = ("glow-layer", "detail-layer")


def init_qt() -> None:
    """建立离屏 QGuiApplication，并把数字/区域格式固定为 en_US。"""
    # zh_HK 等 locale 会让 Qt 用 CJK 数字格式，统一成 en_US 避免意外
    QLocale.setDefault(QLocale("en_US"))
    if QGuiApplication.instance() is None:
        QGuiApplication(sys.argv)
    # offscreen 平台下 QFontDatabase 里没有任何系统字体（预览图文字会变成方框），
    # 需要显式加载字体文件。缺字体不影响 PNG/ICO 生成，只是预览图少个标注。
    for candidate in (
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).is_file():
            QFontDatabase.addApplicationFont(candidate)


def svg_text_for_size(svg_text: str, size: int) -> str:
    """小尺寸使用精简版 SVG（去掉发光层与网格层）。"""
    if size >= SMALL_SIZE_LIMIT:
        return svg_text
    out = svg_text
    for layer in STRIPPED_LAYERS_SMALL:
        out = re.sub(r'<g id="%s">.*?</g>' % re.escape(layer), "", out, flags=re.S)
    return out


def render_svg(renderer: QSvgRenderer, size: int) -> QImage:
    """按目标像素尺寸矢量光栅化，输出 32 位带 alpha 图像。"""
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(0.0, 0.0, float(size), float(size)))
    painter.end()
    return img


def build_pngs(svg_text: str) -> list[tuple[int, QImage, bytes]]:
    """渲染全部尺寸并编码为 PNG 字节。"""
    results: list[tuple[int, QImage, bytes]] = []
    for size in PNG_SIZES:
        data = QByteArray(svg_text_for_size(svg_text, size).encode("utf-8"))
        renderer = QSvgRenderer(data)
        if not renderer.isValid():
            raise RuntimeError("SVG 解析失败，QSvgRenderer.isValid() == False")
        img = render_svg(renderer, size)
        path = ASSETS / f"icon_{size}.png"
        if not img.save(str(path), "PNG"):
            raise RuntimeError(f"PNG 写入失败: {path}")
        results.append((size, img, path.read_bytes()))
        print(f"[png] {path.name:<16} {img.width()}x{img.height()}  {len(results[-1][2])} bytes")
    return results


def write_ico(path: Path, entries: list[tuple[int, bytes]]) -> None:
    """手写 ICO 容器：6 字节头 + N x 16 字节目录项 + 各尺寸 PNG 原始数据。"""
    count = len(entries)
    header = struct.pack("<HHH", 0, 1, count)  # reserved, type=1(icon), count
    offset = 6 + 16 * count  # 数据区起点
    directory = bytearray()
    payload = bytearray()
    for size, blob in entries:
        # 目录项：宽、高（256 记为 0）、调色板色数、保留位、色平面、位深、
        #         数据长度、数据偏移。全部小端。
        dim = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset
        )
        payload += blob
        offset += len(blob)
    path.write_bytes(bytes(header) + bytes(directory) + bytes(payload))


def verify_ico(path: Path) -> bool:
    """读回并解析 ICO，检查目录项、偏移、长度与文件总长是否自洽。"""
    blob = path.read_bytes()
    total = len(blob)
    reserved, img_type, count = struct.unpack("<HHH", blob[:6])
    print(f"\n[ico] {path.name}: {total} bytes")
    print(f"[ico] header: reserved={reserved} type={img_type} count={count}")
    print("[ico] idx  w    h    planes bpp  bytes    offset  end     embedded-png-dims")

    ok = True
    if reserved != 0 or img_type != 1:
        print("[ico] !! 文件头非法")
        ok = False

    expected_offset = 6 + 16 * count
    for i in range(count):
        base = 6 + i * 16
        w, h, colors, res, planes, bpp, length, data_off = struct.unpack(
            "<BBBBHHII", blob[base : base + 16]
        )
        actual_w = 256 if w == 0 else w
        actual_h = 256 if h == 0 else h
        chunk = blob[data_off : data_off + length]

        flags = []
        if colors != 0 or res != 0:
            flags.append("目录项保留字段非 0")
        if planes != 1 or bpp != 32:
            flags.append("planes/bitCount 异常")
        if data_off != expected_offset:
            flags.append(f"偏移不连续(期望 {expected_offset})")
        if length != len(chunk):
            flags.append("数据长度越界")

        png_dims = "-"
        if chunk[:8] == b"\x89PNG\r\n\x1a\n":
            pw = int.from_bytes(chunk[16:20], "big")
            ph = int.from_bytes(chunk[20:24], "big")
            png_dims = f"{pw}x{ph}"
            if (pw, ph) != (actual_w, actual_h):
                flags.append(f"内嵌 PNG 尺寸 {pw}x{ph} 与目录项 {actual_w}x{actual_h} 不符")
        else:
            flags.append("数据不是 PNG（前 8 字节签名不符）")

        status = "OK" if not flags else "!! " + "; ".join(flags)
        if flags:
            ok = False
        print(
            f"[ico] {i:>3}  {actual_w:>3}  {actual_h:>3}  {planes:>6} {bpp:>3}  "
            f"{length:>7}  {data_off:>6}  {data_off + length:>6}  {png_dims:>10}   {status}"
        )
        expected_offset = data_off + length

    if expected_offset != total:
        print(f"[ico] !! 数据区结束于 {expected_offset}，文件长度 {total} 不一致")
        ok = False
    else:
        print(f"[ico] 数据区结束 = 文件长度 = {total}  OK")

    sizes = []
    for i in range(count):
        base = 6 + i * 16
        w = blob[base]
        sizes.append(256 if w == 0 else w)
    print(f"[ico] 尺寸列表: {sizes}")

    # 再用 Qt 自己的 ICO 读取器验一遍（模拟 exe/窗口真正加载图标时的行为）
    icon = QIcon(str(path))
    qt_sizes = sorted((s.width(), s.height()) for s in icon.availableSizes())
    print(f"[ico] QIcon.availableSizes() = {qt_sizes}")
    probe = icon.pixmap(32, 32)
    if probe.isNull() or probe.width() != 32:
        print("[ico] !! QIcon 无法在 32x32 下解码该 ICO")
        ok = False
    else:
        print(f"[ico] QIcon.pixmap(32,32) -> {probe.width()}x{probe.height()}  OK")
    if len(qt_sizes) != count:
        print(f"[ico] !! Qt 读出的尺寸数 {len(qt_sizes)} 与目录项 {count} 不一致")
        ok = False
    return ok


def verify_pngs(pngs: list[tuple[int, QImage, bytes]]) -> bool:
    """读回 PNG，确认尺寸与 alpha 通道。"""
    print("\n[png] 读回校验：")
    ok = True
    for size, _img, _blob in pngs:
        path = ASSETS / f"icon_{size}.png"
        got = QImage(str(path))
        if got.isNull():
            print(f"[png] !! {path.name} 无法读取")
            ok = False
            continue
        has_alpha = got.hasAlphaChannel()
        fmt = got.format().name if hasattr(got.format(), "name") else str(got.format())
        # 统计非全透明像素占比，确认不是空白图
        opaque = 0
        for y in range(got.height()):
            for x in range(got.width()):
                if (got.pixel(x, y) >> 24) & 0xFF:
                    opaque += 1
        ratio = opaque / (got.width() * got.height())
        if (got.width(), got.height()) != (size, size):
            print(f"[png] !! {path.name} 尺寸应为 {size}x{size}，实际 {got.width()}x{got.height()}")
            ok = False
        if not has_alpha:
            print(f"[png] !! {path.name} 无 alpha 通道")
            ok = False
        if ratio < 0.5:
            print(f"[png] !! {path.name} 有效像素仅 {ratio:.1%}，疑似空白")
            ok = False
        print(
            f"[png] {path.name:<16} {got.width()}x{got.height()}  alpha={has_alpha}  "
            f"format={fmt}  非透明像素={ratio:.1%}"
        )
    return ok


def make_preview(pngs: list[tuple[int, QImage, bytes]]) -> None:
    """拼一张对照图：256 原图、小尺寸整数倍放大（最近邻）、浅色底观感。"""
    by_size = {size: img for size, img, _ in pngs}
    cell_w, cell_h = 158, 138
    cols = 3
    grid_x, grid_y = 322, 24
    text_h = 24
    left_bottom = 24 + 256 + text_h
    grid_bottom = grid_y + cell_h * 2
    light_top = max(left_bottom, grid_bottom) + 18
    width = grid_x + cell_w * cols + 26
    height = light_top + 118

    sheet = QImage(width, height, QImage.Format.Format_ARGB32)
    sheet.fill(QColor("#14181c"))

    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setFont(QFont("Segoe UI", 9))
    text_color = QColor("#8b97a5")

    # 左侧：256 原图（暗色底观感）
    painter.drawImage(24, 24, by_size[256])
    painter.setPen(text_color)
    painter.drawText(
        QRectF(24, 24 + 256 + 4, 300, 18),
        int(Qt.AlignmentFlag.AlignLeft),
        "icon_256.png  256x256  on dark #1b1f24",
    )

    # 右侧：小尺寸整数倍放大（最近邻），检查 16px 可辨认度
    small_sizes = [16, 24, 32, 48, 64, 128]
    for idx, size in enumerate(small_sizes):
        factor = max(1, 128 // size)
        col, row = idx % cols, idx // cols
        x = grid_x + col * cell_w
        y = grid_y + row * cell_h
        shown = by_size[size].scaled(
            size * factor,
            size * factor,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        painter.drawImage(x, y, shown)
        painter.setPen(text_color)
        painter.drawText(
            QRectF(x, y + size * factor + 6, cell_w, 18),
            int(Qt.AlignmentFlag.AlignLeft),
            f"{size}x{size}  shown x{factor}",
        )

    # 底部条：浅色背景对照（检查透明边缘与对比度）
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#dfe6ee"))
    painter.drawRoundedRect(QRectF(24, light_top, width - 50, 94), 8, 8)

    big = by_size[256].scaled(
        70, 70, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    painter.drawImage(40, light_top + 12, big)
    x = 128
    for size in (64, 48, 32, 24, 16):
        painter.drawImage(x, light_top + 47 - size // 2, by_size[size])
        x += size + 20
    painter.setPen(QColor("#22272e"))
    painter.drawText(
        QRectF(40, light_top + 88, 260, 18),
        int(Qt.AlignmentFlag.AlignLeft),
        "on light #dfe6ee",
    )
    painter.drawText(
        QRectF(128, light_top + 88, 400, 18),
        int(Qt.AlignmentFlag.AlignLeft),
        "native pixels: 64 / 48 / 32 / 24 / 16",
    )

    painter.end()
    if not sheet.save(str(PREVIEW_PATH), "PNG"):
        raise RuntimeError(f"预览图写入失败: {PREVIEW_PATH}")
    print(
        f"\n[preview] {PREVIEW_PATH.name}: {sheet.width()}x{sheet.height()}  "
        f"{PREVIEW_PATH.stat().st_size} bytes"
    )


def main() -> int:
    init_qt()
    if not SVG_PATH.is_file():
        print(f"缺少 SVG 源文件: {SVG_PATH}")
        return 2
    ASSETS.mkdir(parents=True, exist_ok=True)

    svg_text = SVG_PATH.read_text(encoding="utf-8")
    pngs = build_pngs(svg_text)

    write_ico(ICO_PATH, [(size, blob) for size, _img, blob in pngs])
    ico_ok = verify_ico(ICO_PATH)

    # ICO 内嵌数据必须与独立 PNG 文件逐字节一致
    embedded = []
    blob = ICO_PATH.read_bytes()
    count = struct.unpack("<HHH", blob[:6])[2]
    for i in range(count):
        _w, _h, _c, _r, _p, _b, length, off = struct.unpack("<BBBBHHII", blob[6 + i * 16 : 22 + i * 16])
        embedded.append(blob[off : off + length])
    same = all(e == p for e, (_s, _i, p) in zip(embedded, pngs))
    print(f"[ico] 内嵌数据与独立 PNG 逐字节一致: {same}")
    ico_ok = ico_ok and same and len(embedded) == len(pngs)

    png_ok = verify_pngs(pngs)
    make_preview(pngs)

    print("\n[result]", "全部校验通过" if (ico_ok and png_ok) else "存在校验失败项")
    return 0 if (ico_ok and png_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
