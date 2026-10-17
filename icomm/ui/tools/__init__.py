"""工具注册表：这里 import 过的工具才会出现在「工具」菜单里。

新增一个工具 = 在本目录新建模块 + 在这里加一行 import。
菜单的分组与排序由 :mod:`icomm.ui.tools.base` 的注册表负责，界面代码不用改。
"""

from .base import (  # noqa: F401
    DialogTool,
    Tool,
    ToolContext,
    WindowTool,
    find_tool,
    register_tool,
    tool_names,
    tools,
    tools_by_group,
)

# --------------------------------------------------------------- 内置工具
# 各模块在自己的文件里用 @register_tool 注册；这里只负责"让它们被 import 到"。
# 用 try/except 包一层：某个工具依赖缺失时不该让整个程序起不来。

# 计算类
try:
    from .programmer_calc import ProgrammerCalcTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass

# 通信类
try:
    from .modbus_frame import ModbusFrameTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass
try:
    from .connectivity import ConnectivityTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass

# 工程类
try:
    from .variable_search import VariableSearchTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass
try:
    from .notepad import NotepadTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass
try:
    from .param_record import ParamRecordTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass

# 自动化类
try:
    from .screenshot import ScreenshotTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass
try:
    from .screen_record import ScreenRecordTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass
try:
    from .datalog import DataLogTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass
try:
    from .timing import TimingTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass
try:
    from .fault_record import FaultRecordTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass
try:
    from .audio_record import AudioRecordTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass
try:
    from .diagram_edit import DiagramEditTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass

# 计算类（分析工具）
try:
    from .analysis import AnalysisTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass
try:
    from .response_time import ResponseTimeTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass

# 观察类
try:
    from .mini_scope import MiniScopeTool  # noqa: F401
except ImportError:  # pragma: no cover
    pass

