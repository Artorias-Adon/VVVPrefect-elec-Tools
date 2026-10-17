# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

用法（在工程根目录执行）::

    pyinstaller --noconfirm VVPhmi.spec

产物::

    dist/VVPhmi/VVPhmi.exe      # 无控制台窗口
    dist/VVPhmi/               # 整个目录一起拷到工控机即可运行

要点说明：
* ``icomm`` / ``tools`` 是源码包，会被打进 PYZ，**不要**用 --add-data 当数据文件拷；
* ``tools.modbus_sim`` 是在函数内 import 的（内置仿真器），显式声明为 hiddenimport 更稳；
* 剔除了 QtWebEngine / Qml / 3D / Charts / Multimedia 等用不到的大块头，
  否则产物体积会从 ~60MB 膨胀到 ~250MB；
* ``uplx=False``：工业现场常带杀毒软件，加壳容易被误报。
"""

from PyInstaller.utils.hooks import collect_submodules

import os
import re

# 版本号与说明文字只在 icomm/__init__.py 里写一次，这里读出来写进 exe 的文件属性，
# 这样资源管理器"属性 → 详细信息"里能看到版本与说明
_versionitem: dict[str, str] = {}
with open(os.path.join("icomm", "__init__.py"), encoding="utf-8") as fh:
    for line in fh:
        if line.startswith(("__version__", "APP_DESCRIPTION")):
            exec(line, _versionitem)  # noqa: S102 - 读自己仓库里的版本号/说明
APP_VERSION = _versionitem.get("__version__", "0.0.0")
APP_DESCRIPTION = _versionitem.get("APP_DESCRIPTION", "VVPhmi")
_version_tuple = tuple(int(part) for part in re.findall(r"\d+", APP_VERSION)[:4]) or (0, 0, 0)
_version_tuple = (_version_tuple + (0, 0, 0, 0))[:4]

_version_file = os.path.join("build", "version_info.txt")
os.makedirs("build", exist_ok=True)
with open(_version_file, "w", encoding="utf-8") as fh:
    fh.write(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_version_tuple},
    prodvers={_version_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404b0',
        [StringStruct('CompanyName', 'VVPhmi'),
         StringStruct('FileDescription', '{APP_DESCRIPTION}'),
         StringStruct('FileVersion', '{APP_VERSION}'),
         StringStruct('InternalName', 'VVPhmi'),
         StringStruct('LegalCopyright', ''),
         StringStruct('OriginalFilename', 'VVPhmi.exe'),
         StringStruct('ProductName', 'VVPhmi 超棒的上位机'),
         StringStruct('ProductVersion', '{APP_VERSION}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    )

#: 各协议驱动跟着依赖走；IEC 61850 的 Python 绑定只有 Windows 预编译包
hiddenimports = [
    "tools.modbus_sim",
    "tools.can_sim",
    "tools.iec61850_sim",
    "tools.s7_sim",
    "serial",
    "serial.tools.list_ports",
    "can",
    "iec61850",
    "snap7",
]
hiddenimports += collect_submodules("pymodbus")

excludes = [
    # GUI 用不到的大组件
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtTest",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtNetworkAuth",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",  # 串口走 pyserial，不用 Qt 的
    "PySide6.QtBluetooth",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtHelp",
    "PySide6.QtUiTools",
    # 与本程序无关的 Python 包
    "tkinter",
    "unittest",
    "pytest",
    "numpy",
    "matplotlib",
    "PIL",
    "setuptools",
    "pip",
]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    # 图标等资源打进包，运行时由 icomm/ui/resources.py 定位
    datas=[
    ("assets/icon.ico", "assets"),
    ("assets/icon_256.png", "assets"),
    ("assets/sim_iec61850.icd", "assets"),
],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# PyInstaller 的 PySide6 hook 会按 Qt 插件依赖把 Qml/Quick/Pdf 等库一并收进来，
# 只靠 excludes 挡 Python 模块是挡不住这些二进制依赖的，需要在 Analysis 之后手工过滤。
# 本程序是纯 QPainter 光栅绘图，不用 OpenGL，故 opengl32sw.dll 也可剔除。
#
# 三个平台的库文件名不一样，同一套正则都要认：
#   Windows  PySide6\Qt6Qml.dll
#   macOS    PySide6/Qt/lib/libQt6Qml.6.dylib、QtQml.framework/Versions/A/QtQml
#   Linux    PySide6/Qt/lib/libQt6Qml.so.6
_DROP_MODULES = r"(?:Qml\w*|Quick\w*|Pdf\w*|Designer|Multimedia\w*|Charts|DataVisualization|3D\w*)"
_DROP_BINARY = re.compile(
    rf"(?:^|[\\/])(?:lib)?Qt6?{_DROP_MODULES}(?:\.\w+)?(?:\.dll|\.dylib|\.so\w*|[\\/]|$)"
    r"|opengl32sw|d3dcompiler_\d+",
    re.IGNORECASE,
)
_DROP_DATA = re.compile(r"PySide6[\\/]Qt[\\/](?:qml|quick)[\\/]", re.IGNORECASE)

a.binaries = [entry for entry in a.binaries if not _DROP_BINARY.search(entry[0])]
a.datas = [
    entry
    for entry in a.datas
    if not _DROP_DATA.search(entry[0]) and not _DROP_BINARY.search(entry[0])
]

pyz = PYZ(a.pure)

_exe_icon = "assets/icon.ico" if os.name == "nt" else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VVPhmi",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 上位机不要黑窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 可执行文件本身的图标（资源管理器里显示的那个；macOS 用 BUNDLE 的 .icns）
    icon=_exe_icon,
    # 资源管理器"属性 → 详细信息"里的版本号（由上面的 APP_VERSION 生成；Windows 专用）
    version=_version_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VVPhmi",
)

# --------------------------------------------------------------- macOS（.app）
# Windows 上是"一个目录 + exe"，macOS 要的是 .app 包（里面才是那个可执行文件）。
# 只有真的在 macOS 上打包时才会走到这里 —— PyInstaller 不能交叉编译，
# 发布用的 .dmg 由 GitHub Actions 的 macOS 机器构建（见 .github/workflows/macos-dmg.yml）。
import sys  # noqa: E402  (放在这里不影响 Windows 构建路径)

if sys.platform == "darwin":
    _icns = "assets/icon.icns"
    app = BUNDLE(
        coll,
        name="VVPhmi.app",
        icon=_icns if os.path.exists(_icns) else None,
        bundle_identifier="io.github.artorias-adon.vvphmi",
        info_plist={
            "CFBundleName": "VVPhmi",
            "CFBundleDisplayName": "VVPhmi 上位机",
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "NSHighResolutionCapable": True,
            # 录屏/截屏要系统授权，这里写清用途，第一次弹窗时用户知道为什么
            "NSCameraUsageDescription": "屏幕录制工具需要录屏权限",
            "LSApplicationCategoryType": "public.app-category.developer-tools",
            "LSMinimumSystemVersion": "11.0",
        },
    )

