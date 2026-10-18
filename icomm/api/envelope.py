"""请求 / 响应信封 —— 外部脚本与各传输层共用的唯一数据形状。

为什么先定这一层：以后无论是 HTTP、WebSocket、命名管道还是本地 stdin/stdout，
**线上传的都是这里的两种 dict**，服务端只做三件事：

1. 把收到的字节解成 :class:`ApiRequest`（``ApiRequest.from_dict``）；
2. 交给 :class:`icomm.api.ApiService` 调用（``service.handle``）；
3. 把 :class:`ApiResponse` 序列化回去（``response.to_dict``）。

这样"协议"和"业务"就分开了：加一种传输方式不用碰任何业务代码，
改一个方法也不用碰任何传输代码。版本号是给**以后的兼容**留的口子 ——
客户端带着比自己认识的更高的 ``version`` 来，服务端会明确回 ``unsupported``，
而不是胡乱解释参数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 当前协议版本。改方法签名、改字段含义时**加**这个数字，别偷偷改。
API_VERSION = 1

# --------------------------------------------------------------- 错误码
# 错误码是给脚本判断用的（字符串稳定），message 是给人看的（可以改）。

OK = "ok"
BAD_REQUEST = "bad_request"
UNKNOWN_METHOD = "unknown_method"
NOT_FOUND = "not_found"
NOT_READY = "not_ready"
DENIED = "denied"
TIMEOUT = "timeout"
UNSUPPORTED = "unsupported"
INTERNAL = "internal"

#: 错误码 -> 中文说明（文档与自检用；脚本里按左边的字符串判断）
ERROR_CODES: dict[str, str] = {
    OK: "成功",
    BAD_REQUEST: "请求本身不对：缺参数、参数类型不对、多了不认识的参数",
    UNKNOWN_METHOD: "没有这个方法（用 api.describe 看有哪些）",
    NOT_FOUND: "点位 / 设备 / 记录不存在",
    NOT_READY: "现在还做不了这件事：采集没启动、通道没运行之类",
    DENIED: "不允许：只读模式、点位不可写",
    TIMEOUT: "等设备回应超时",
    UNSUPPORTED: "协议版本比服务端高，先升级服务端",
    INTERNAL: "服务端内部出错（细节见 detail）",
}


class ApiError(Exception):
    """带错误码的异常：服务层把它原样翻成错误响应。

    业务代码只管 ``raise ApiError(NOT_FOUND, "没有这个点位")``，
    不用关心最后是 JSON 还是别的什么形式返回给调用者。
    """

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            data["detail"] = self.detail
        return data

    def __str__(self) -> str:  # pragma: no cover - 只为日志好看
        return f"[{self.code}] {self.message}"


# --------------------------------------------------------------- 请求


@dataclass
class ApiRequest:
    """一次调用。``id`` 由调用方给，原样回传（异步/批量时用它配对）。"""

    method: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    version: int = API_VERSION

    @classmethod
    def from_dict(cls, data: Any) -> "ApiRequest":
        if not isinstance(data, dict):
            raise ApiError(BAD_REQUEST, "请求必须是一个对象（JSON object）")
        method = data.get("method", "")
        if not isinstance(method, str) or not method.strip():
            raise ApiError(BAD_REQUEST, "缺少 method（要调用的方法名）")
        params = data.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ApiError(BAD_REQUEST, "params 必须是对象；没有参数就传 {} 或省略")
        version = data.get("version", API_VERSION)
        if not isinstance(version, int) or isinstance(version, bool):
            raise ApiError(BAD_REQUEST, "version 必须是整数")
        if version > API_VERSION:
            raise ApiError(
                UNSUPPORTED,
                f"请求协议版本 {version} 高于服务端的 {API_VERSION}",
                {"server_version": API_VERSION},
            )
        request_id = data.get("id", "")
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            raise ApiError(BAD_REQUEST, "id 只能是字符串或整数")
        return cls(method=method.strip(), params=dict(params), id=str(request_id), version=version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "method": self.method,
            "params": dict(self.params),
            "version": self.version,
        }


# --------------------------------------------------------------- 响应


@dataclass
class ApiResponse:
    """一次调用的结果。``ok=False`` 时 ``result`` 为空、``error`` 有内容。"""

    id: str = ""
    ok: bool = True
    result: Any = None
    error: dict[str, Any] | None = None
    version: int = API_VERSION

    @classmethod
    def success(cls, request: ApiRequest | None = None, result: Any = None) -> "ApiResponse":
        return cls(
            id=request.id if request else "",
            ok=True,
            result=result,
            version=request.version if request else API_VERSION,
        )

    @classmethod
    def failure(cls, request: ApiRequest | None, error: ApiError) -> "ApiResponse":
        return cls(
            id=request.id if request else "",
            ok=False,
            result=None,
            error=error.to_dict(),
            version=API_VERSION,
        )

    @property
    def code(self) -> str:
        """``ok`` 或具体错误码，方便脚本一行判断。"""
        return OK if self.ok else str((self.error or {}).get("code", INTERNAL))

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "ok": self.ok, "version": self.version}
        if self.ok:
            data["result"] = self.result
        else:
            data["error"] = self.error or {"code": INTERNAL, "message": "未知错误"}
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "ApiResponse":
        if not isinstance(data, dict):
            raise ApiError(BAD_REQUEST, "响应必须是一个对象（JSON object）")
        return cls(
            id=str(data.get("id", "")),
            ok=bool(data.get("ok", False)),
            result=data.get("result"),
            error=data.get("error"),
            version=int(data.get("version", API_VERSION)),
        )


__all__ = [
    "API_VERSION",
    "BAD_REQUEST",
    "DENIED",
    "ERROR_CODES",
    "INTERNAL",
    "NOT_FOUND",
    "NOT_READY",
    "OK",
    "TIMEOUT",
    "UNKNOWN_METHOD",
    "UNSUPPORTED",
    "ApiError",
    "ApiRequest",
    "ApiResponse",
]
