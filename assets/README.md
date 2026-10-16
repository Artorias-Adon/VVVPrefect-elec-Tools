# iComm 图标资产

## 图标含义

| 元素 | 颜色 | 含义 |
| --- | --- | --- |
| 圆角方形底板 + 网格刻度 | `#1b1f24` / `#22272e` | 深色工业风上位机底座（网格=刻度/工程感） |
| 青绿电气波形（带发光描边） | `#2f9e6b` | 交流侧电压/电流采样波形，"带电/实时" |
| 蓝色直流母线（带高光条） | `#3d8bfd` | 整流后的直流母线 |

组合起来表达 **交流波形 → 整流 → 直流母线** 这条整流器 / 配电监控主线；配色与
`icomm/ui/theme.py` 一致。图形为原创抽象符号，不含任何模仿第三方商标的元素。

构图刻意只保留"一条粗线 + 一个粗色块"两个大元素，保证 16x16 下仍可辨认；
发光与网格属于低对比装饰，小尺寸渲染时会被脚本自动删除。

## 文件清单

| 文件 | 用途 |
| --- | --- |
| `icon.svg` | 矢量源文件（手写，可直接编辑）。**改设计只改这里** |
| `icon_16.png` … `icon_256.png` | 7 个尺寸的 32 位带 alpha PNG，由 SVG 在各目标像素尺寸上矢量直出 |
| `icon.ico` | Windows 图标，内嵌 16/24/32/48/64/128/256 共 7 个尺寸的 PNG；给 PyInstaller 打包的 exe 和 QT 窗口图标使用 |
| `icon_preview.png` | 对照图（256 原图 + 小尺寸整数倍放大 + 浅色底观感），仅供人工检查，程序不引用 |

## 重新生成

在工程根目录执行：

```powershell
python tools/make_icon.py
```

脚本会覆盖生成 `assets/` 下的 7 个 PNG、`icon.ico` 和 `icon_preview.png`，并打印校验结果。

脚本要点：

* 只依赖 **PySide6**（`QtSvg` + `QtGui`），不需要 Pillow；SVG → PNG 用 `QSvgRenderer`
  直接按目标像素光栅化，小尺寸比"先渲染 256 再缩小"清晰得多；
* 无显示器环境可运行（脚本自己设置 `QT_QPA_PLATFORM=offscreen`），并把 QLocale 固定为
  `en_US` 以避免 zh_HK 等 locale 影响数字格式；
* 渲染 16 / 24 px 时会删掉 SVG 里的 `glow-layer`（发光）与 `detail-layer`（网格），
  只保留底板与粗线条主体，避免小尺寸糊成噪点；
* `.ico` 为手写 ICO 容器：6 字节文件头（reserved=0, type=1, count=7）+ 7 个 16 字节目录项
  （宽高 256 写作 0、planes=1、bitCount=32、数据长度与偏移，全部小端）+ 顺序拼接的 PNG 原始数据
  （Vista 及以上支持 ICO 内嵌 PNG）；
* 运行结束会自检：ICO 目录项/偏移/长度与文件总长自洽、内嵌 PNG 与独立 PNG 逐字节一致、
  每个 PNG 的尺寸与 alpha 通道、以及 `QIcon` 能否解码出全部 7 个尺寸。

> 注：脚本内的中文说明为 UTF-8；在部分 Windows 控制台代码页下打印的中文可能显示为乱码，
> 不影响生成结果。

## 集成提示（本目录文件目前未被任何代码引用，集成请自行完成）

```python
# iComm.spec —— 打包出的 exe 图标
exe = EXE(..., icon="assets/icon.ico")

# main.py —— 窗口/任务栏图标
from PySide6.QtGui import QIcon
app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "icon.ico")))
```

Windows 任务栏若要显示独立图标（而不是 python.exe 的图标），通常还需要设置 AppUserModelID。
