"""采集引擎与 Qt 界面之间的桥接。

引擎在普通 Python 线程里跑，通过这里把回调转成 Qt 信号，
由 Qt 自动以队列连接投递到界面线程，避免跨线程操作控件。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class EngineBridge(QObject):
    """把引擎回调转成 Qt 信号。"""

    tagsUpdated = Signal(dict)
    deviceStateChanged = Signal(object)
    alarmRaised = Signal(object)
    frameReceived = Signal(str, str, str)
    logReceived = Signal(str, str, str)

    def attach(self, engine) -> None:
        """把本桥接对象挂到引擎上。"""
        engine.on_tags = self.tagsUpdated.emit
        engine.on_device_state = self.deviceStateChanged.emit
        engine.on_alarm = self.alarmRaised.emit
        engine.on_frame = self.frameReceived.emit
        engine.on_log = self.logReceived.emit

    def detach(self, engine) -> None:
        engine.on_tags = None
        engine.on_device_state = None
        engine.on_alarm = None
        engine.on_frame = None
        engine.on_log = None
