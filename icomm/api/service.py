"""API 服务：方法是它调的，参数是它校验的，事件是它发的。

它**不认识任何传输方式**（HTTP / WebSocket / 管道 / 界面内部调用都一样）：
上层只要能给出一个 :class:`ApiRequest`，就能拿到一个 :class:`ApiResponse`。
这里刻意把三件事做全：

* **校验**：缺参数、类型不对、多传了不认识的参数都直接 ``bad_request``。
  多传参数也报错是有意的 —— 脚本里把 ``pid`` 写成 ``point`` 时，
  静默忽略只会让人查半天"为什么没生效"。
* **只读开关**：``read_only=True`` 时任何 ``mutates=True`` 的方法一律 ``denied``。
  这是给未来的外部接口留的安全阀（**写值会真的动设备**，默认应当只读）。
* **事件订阅**：引擎回调 → 事件 → 订阅者。回调在引擎线程里执行，
  订阅者自己负责投递（界面要转成 Qt 信号，脚本要加锁），这里不替谁做线程切换。
"""

from __future__ import annotations

import json
import threading
import traceback
from typing import Any, Callable

from .envelope import (
    API_VERSION,
    BAD_REQUEST,
    DENIED,
    INTERNAL,
    UNKNOWN_METHOD,
    UNSUPPORTED,
    ApiError,
    ApiRequest,
    ApiResponse,
)
from .registry import MethodRegistry, MethodSpec

# --------------------------------------------------------------- 事件名
# 这些是"推"出去的：订阅一次，之后每来一批数据就回调一次。

#: 一批点位值的更新（``{点号: TagValue}``）
EVENT_TAGS = "tags"
#: 某台设备的通信状态变化（``DeviceStatus``）
EVENT_DEVICE_STATE = "device_state"
#: 报警产生 / 复位（``AlarmEvent``）
EVENT_ALARM = "alarm"
#: 通信报文（方向, 文本, 通道名）
EVENT_FRAME = "frame"
#: 通信日志（级别, 消息, 通道名）
EVENT_LOG = "log"

EVENTS: tuple[str, ...] = (EVENT_TAGS, EVENT_DEVICE_STATE, EVENT_ALARM, EVENT_FRAME, EVENT_LOG)

#: 允许的事件名（写错立刻报错，免得订阅了一个永远不来的事件）
def event_names() -> list[str]:
    return list(EVENTS)


#: 参数类型 -> 期望的 Python 类型（bool 要在 int 之前判断，Python 里 bool 是 int 的子类）
_KIND_TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
    "list": (list, tuple),
    "dict": (dict,),
}


class ApiService:
    """方法注册表 + 调用分发 + 事件总线。"""

    def __init__(self, plant: Any = None, *, read_only: bool | None = None) -> None:
        self.plant = plant
        self.registry = MethodRegistry()
        self._lock = threading.RLock()
        self._subscribers: dict[str, list[Callable[[Any], None]]] = {}
        if read_only is None:
            read_only = bool(getattr(plant, "read_only", False))
        self.read_only = bool(read_only)
        self.last_error = ""

    # -------------------------------------------------- 注册

    def add(self, name: str, func: Callable[..., Any], summary: str = "",
            params: tuple = (), *, mutates: bool = False, returns: str = "") -> None:
        self.registry.add(
            MethodSpec(
                name=name,
                func=func,
                summary=summary,
                params=tuple(params),
                mutates=mutates,
                returns=returns,
            )
        )

    def method(self, name: str, summary: str = "", params: tuple = (), *,
               mutates: bool = False, returns: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """装饰器写法，注册到本服务（内部转发给 :class:`MethodRegistry`）。"""
        return self.registry.method(name, summary, params, mutates=mutates, returns=returns)

    # -------------------------------------------------- 调用

    def call(self, method: str, params: dict[str, Any] | None = None,
             request_id: str = "") -> ApiResponse:
        """调一个方法。**不抛异常**：任何问题都变成 ``ok=False`` 的响应。"""
        request = ApiRequest(method=str(method or ""), params=dict(params or {}), id=str(request_id))
        return self._run(request)

    def handle(self, request: ApiRequest) -> ApiResponse:
        """处理一个已经解好的请求。"""
        return self._run(request)

    def handle_dict(self, payload: Any) -> dict[str, Any]:
        """收一个请求 dict（或 ``{"method":..., "params":...}`` 直接给），回一个响应 dict。

        这是**给传输层用的入口**：HTTP 服务收到 JSON 后调它，取返回值序列化即可；
        连请求都解不出来时也会给一个结构化的错误响应，不会把异常扔给 socket。
        """
        try:
            request = ApiRequest.from_dict(payload)
        except ApiError as error:
            return ApiResponse.failure(None, error).to_dict()
        return self._run(request).to_dict()

    def handle_json(self, text: str) -> str:
        """收一段 JSON 文本，回一段 JSON 文本（调试与本地管道最省事）。"""
        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as error:
            return json.dumps(
                ApiResponse.failure(
                    None, ApiError(BAD_REQUEST, f"请求不是合法 JSON：{error}")
                ).to_dict(),
                ensure_ascii=False,
            )
        return json.dumps(self.handle_dict(payload), ensure_ascii=False)

    def _run(self, request: ApiRequest) -> ApiResponse:
        try:
            if request.version > API_VERSION:
                raise ApiError(
                    UNSUPPORTED,
                    f"请求协议版本 {request.version} 高于服务端的 {API_VERSION}",
                )
            spec = self.registry.get(request.method)
            if spec is None:
                raise ApiError(
                    UNKNOWN_METHOD,
                    f"没有方法 {request.method}",
                    {"known": self.registry.names()},
                )
            if spec.mutates and self.read_only:
                raise ApiError(
                    DENIED,
                    f"当前是只读模式，{spec.name} 会改变现场或磁盘，已拒绝",
                )
            kwargs = self._check_params(spec, request.params)
            result = spec.func(**kwargs)
            return ApiResponse.success(request, result)
        except ApiError as error:
            return ApiResponse.failure(request, error)
        except Exception as error:  # noqa: BLE001 - 服务层必须兜住一切，否则脚本那边只能看到断开
            self.last_error = "".join(
                traceback.format_exception_only(type(error), error)
            ).strip()
            return ApiResponse.failure(
                request,
                ApiError(
                    INTERNAL,
                    f"{type(error).__name__}: {error}",
                    {"traceback": traceback.format_exc(limit=6)},
                ),
            )

    def _check_params(self, spec: Any, params: dict[str, Any]) -> dict[str, Any]:
        allowed = spec.allowed_params
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise ApiError(
                BAD_REQUEST,
                f"{spec.name} 不认识参数 {', '.join(unknown)}",
                {"allowed": sorted(allowed)},
            )
        kwargs: dict[str, Any] = {}
        for param in spec.params:
            if param.name in params:
                value = params[param.name]
                expected = _KIND_TYPES.get(param.kind)
                if expected is not None:
                    if isinstance(value, bool) and param.kind != "bool":
                        raise ApiError(
                            BAD_REQUEST,
                            f"{spec.name}.{param.name} 要 {param.kind}，给的是 bool",
                        )
                    if not isinstance(value, expected):
                        raise ApiError(
                            BAD_REQUEST,
                            f"{spec.name}.{param.name} 要 {param.kind}，"
                            f"给的是 {type(value).__name__}",
                        )
                kwargs[param.name] = value
            elif param.required:
                raise ApiError(
                    BAD_REQUEST,
                    f"{spec.name} 缺少必填参数 {param.name}",
                    {"allowed": sorted(allowed)},
                )
            else:
                kwargs[param.name] = param.default
        return kwargs

    # -------------------------------------------------- 自检 / 发现

    def manifest(self) -> dict[str, Any]:
        """服务端的"能力清单"：版本、方法、事件、只读状态、当前工程。

        未来的传输层可以把这份清单暴露成 ``GET /`` 或 ``api.describe``，
        客户端据此决定能用什么（也能据此做协议版本协商）。
        """
        project = getattr(self.plant, "project", None)
        return {
            "api_version": API_VERSION,
            "read_only": self.read_only,
            "method_count": len(self.registry),
            "methods": self.registry.describe(),
            "events": event_names(),
            "project": {
                "name": getattr(project, "name", ""),
                "devices": len(getattr(project, "devices", []) or []),
                "channels": len(getattr(project, "channels", []) or []),
                "points": len(getattr(project, "all_point_ids", lambda: [])()),
            },
            "engine_running": bool(getattr(getattr(self.plant, "engine", None), "running", False)),
        }

    # -------------------------------------------------- 事件

    def subscribe(self, event: str, callback: Callable[[Any], None]) -> Callable[[], None]:
        """订阅一个事件，返回"退订"函数。

        ``callback(payload)`` 在**引擎线程**里被调用：界面上用要自己投递到主线程，
        脚本里用要自己加锁。这里故意不做线程切换，免得隐藏时序问题。
        """
        if event not in EVENTS:
            raise ApiError(BAD_REQUEST, f"没有事件 {event}", {"known": event_names()})
        if not callable(callback):
            raise ApiError(BAD_REQUEST, "callback 必须可调用")
        with self._lock:
            self._subscribers.setdefault(event, []).append(callback)

        def unsubscribe() -> None:
            self.unsubscribe(event, callback)

        return unsubscribe

    def unsubscribe(self, event: str, callback: Callable[[Any], None]) -> bool:
        with self._lock:
            handlers = self._subscribers.get(event)
            if not handlers:
                return False
            try:
                handlers.remove(callback)
            except ValueError:
                return False
            return True

    def publish(self, event: str, payload: Any) -> int:
        """把事件发给订阅者，返回送达的订阅者数量。

        某个订阅者报错不影响其他订阅者（一个写坏的脚本不该把采集搞挂）。
        """
        with self._lock:
            handlers = list(self._subscribers.get(event, ()))
        delivered = 0
        for handler in handlers:
            try:
                handler(payload)
                delivered += 1
            except Exception:  # noqa: BLE001 - 订阅者自己的问题，记下继续发
                self.last_error = traceback.format_exc(limit=4)
        return delivered

    def subscriber_count(self, event: str) -> int:
        with self._lock:
            return len(self._subscribers.get(event, ()))


__all__ = [
    "EVENTS",
    "EVENT_ALARM",
    "EVENT_DEVICE_STATE",
    "EVENT_FRAME",
    "EVENT_LOG",
    "EVENT_TAGS",
    "ApiService",
    "event_names",
]
