"""VVPhmi —— 电气调试与监视软件平台。

分层：
    icomm.core     数据模型、编解码、驱动框架、采集引擎、报警（不依赖 Qt）
    icomm.storage  SQLite 归档与日志
    icomm.ui       PySide6 界面

.. note::
   Python 包名是历史遗留的 ``icomm``（对外显示名与可执行文件已改成 VVPhmi）。
   包名一旦改了，所有 import、打包配置、现场已部署的目录结构都要跟着变，
   而它对外并不可见，所以保持不动。
"""

__version__ = "1.4.1"

#: 对外显示名（窗口标题、关于对话框、界面标题栏）
APP_NAME = "VVPhmi 超棒(๑•̀ㅂ•́)و✧的上位机！"
#: 短名（可执行文件名、日志前缀等不适合放表情符号的地方）
APP_SHORT = "VVPhmi"
#: 版本号（和 ``__version__`` 同一个值，给打包脚本与界面用）
APP_VERSION = __version__
#: 一句话说明（打包进 exe 的文件属性、关于对话框、发布说明都用它，别各写一份）
APP_DESCRIPTION = "电气调试与监视软件（多协议组态 / 录波示波器 / 现场工具）"
