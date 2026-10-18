"""外部脚本接口（预留）。

现在的样子：**一个与 Qt 无关的门面 + 一套已经能用 JSON 表达的方法表**。
界面和脚本走同一条代码路径（``Plant``），所以今天就能写脚本读值/写值/查历史，
而将来接 HTTP / WebSocket / 命名管道时，只需要实现
:class:`icomm.api.transport.Transport` 并在里面调 ``service.handle_dict()``。

用起来是这样::

    from icomm.api import build_service, open_plant
    from icomm.api.transport import DirectTransport

    with open_plant("现场工程.iprj") as plant:      # 默认只读
        service = build_service(plant)
        api = DirectTransport(service)

        print(api.call("engine.status").result)
        plant.start()                                # 或 api.call("engine.start")
        print(api.call("point.read", pid="PCS.Pset").result)

        # JSON 进、JSON 出（未来网络传输就是这个形状）
        print(service.handle_json('{"method": "point.list", "params": {"writable": true}}'))

        # 事件：引擎线程回调，脚本里自己加锁
        service.subscribe("tags", lambda values: print(len(values)))

约定三条（都是为了让"以后能安全地对外开"）：

* **默认只读**：``open_plant()`` 不给 ``read_only=False`` 就不许写值；
* **方法自带元数据**：参数、是否写操作都在注册表里，未来鉴权/审计按它来；
* **错误码稳定**：脚本按 ``error.code`` 判断，``message`` 只给人看。
"""

from .envelope import (
    API_VERSION,
    BAD_REQUEST,
    DENIED,
    ERROR_CODES,
    INTERNAL,
    NOT_FOUND,
    NOT_READY,
    OK,
    TIMEOUT,
    UNKNOWN_METHOD,
    UNSUPPORTED,
    ApiError,
    ApiRequest,
    ApiResponse,
)
from .methods import build_service, method_names
from .plant import Plant, open_plant
from .registry import KINDS, MethodRegistry, MethodSpec, ParamSpec, optional, required
from .service import (
    EVENTS,
    EVENT_ALARM,
    EVENT_DEVICE_STATE,
    EVENT_FRAME,
    EVENT_LOG,
    EVENT_TAGS,
    ApiService,
    event_names,
)
from .transport import DirectTransport, Transport

__all__ = [
    "API_VERSION",
    "BAD_REQUEST",
    "DENIED",
    "ERROR_CODES",
    "EVENTS",
    "EVENT_ALARM",
    "EVENT_DEVICE_STATE",
    "EVENT_FRAME",
    "EVENT_LOG",
    "EVENT_TAGS",
    "INTERNAL",
    "KINDS",
    "NOT_FOUND",
    "NOT_READY",
    "OK",
    "TIMEOUT",
    "UNKNOWN_METHOD",
    "UNSUPPORTED",
    "ApiError",
    "ApiRequest",
    "ApiResponse",
    "ApiService",
    "DirectTransport",
    "MethodRegistry",
    "MethodSpec",
    "ParamSpec",
    "Plant",
    "Transport",
    "build_service",
    "event_names",
    "method_names",
    "open_plant",
    "optional",
    "required",
]
