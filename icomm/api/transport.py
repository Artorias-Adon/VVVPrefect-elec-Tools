"""传输层契约 —— **这里故意不实现任何服务端**，只把接口先定下来。

为什么先定：写值能真的动设备，所以"外部脚本怎么连进来"必须先想清楚三件事，
否则以后每加一种传输方式都要重新讨论一遍安全边界：

1. **鉴权归传输层**：谁能连、要不要口令、从哪个网卡进来，是传输层决定的；
   服务层只有一个"只读/可写"总闸（``ApiService.read_only``）。
   传输层要在收到第一个请求时按自己的策略决定这个 ``ApiService`` 是否可写。
2. **序列化只用信封**：传输层不许自己拼响应，一律
   ``service.handle_dict(载荷) -> dict`` 或 ``service.handle_json(文本) -> 文本``。
   这样所有客户端的错误码、版本字段都是同一套。
3. **生命周期归调用方**：传输层不负责采集，也不该 long-running 地持有工程锁；
   它只是把请求搬进来、把响应搬出去。

将来实现时（HTTP / WebSocket / 命名管道 / stdin-stdout）只需要::

    class HttpTransport:
        name = "http"

        def __init__(self, service, host="127.0.0.1", port=0):
            self.service = service
            ...

        def serve(self) -> None:      # 阻塞或后台线程里跑
            ...
            payload = json.loads(body)
            reply = self.service.handle_dict(payload)     # ← 唯一入口
            ...send json.dumps(reply)

        def shutdown(self) -> None:
            ...

剩下的（方法表、参数校验、只读拦截、错误码、事件订阅）都已经在
:class:`icomm.api.ApiService` 里，不用再写第二遍。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .envelope import ApiRequest, ApiResponse
from .service import ApiService


@runtime_checkable
class Transport(Protocol):
    """所有传输方式的共同点：有个名字，能起服务，能停。"""

    #: 传输名（``http`` / ``ws`` / ``pipe`` / ``inproc``），出现在日志与能力清单里
    name: str

    def serve(self, service: ApiService) -> None:
        """开始服务。阻塞与否由实现决定，但必须能被 :meth:`shutdown` 叫停。"""
        ...

    def shutdown(self) -> None:
        """停止服务（释放端口/句柄，结束线程）。要允许重复调用。"""
        ...


class DirectTransport:
    """进程内直连：同机脚本最省事的用法，也是测试里用的那个。

    ``DirectTransport(service).call("point.read", pid="PCS.Pset")``
    —— 跟未来的网络传输走的是**同一个** ``ApiService``，行为一模一样。
    """

    name = "inproc"

    def __init__(self, service: ApiService) -> None:
        self.service = service
        self._serving = False

    def serve(self, service: ApiService | None = None) -> None:  # pragma: no cover - 无实际动作
        if service is not None:
            self.service = service
        self._serving = True

    def shutdown(self) -> None:  # pragma: no cover - 无实际动作
        self._serving = False

    def call(self, method: str, **params: Any) -> ApiResponse:
        """按方法名 + 关键字参数调一次，拿到完整响应（``.ok`` / ``.result`` / ``.code``）。"""
        return self.service.call(method, params)

    def call_dict(self, method: str, **params: Any) -> dict[str, Any]:
        """同上，但直接给可 JSON 化的 dict（脚本里再存文件/转发最方便）。"""
        return self.service.call(method, params).to_dict()

    def request(self, request: ApiRequest) -> ApiResponse:
        return self.service.handle(request)


__all__ = ["DirectTransport", "Transport"]
