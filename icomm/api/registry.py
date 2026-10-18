"""方法注册表：每个可被外部脚本调用的能力都在这里登记一次。

登记的不只是函数，还有**参数说明**和**是不是写操作**。这两样是给以后留的接口：

* 参数说明 → 未来的 ``api.describe`` / 帮助页 / 自动生成的脚本模板都从这儿读，
  不用再写一份文档（写两份一定会不同步）；
* ``mutates=True`` → 传输层要鉴权、只读模式要拦、审计日志要记 —— 现在就在每个
  方法上标清楚，将来加 HTTP 服务时不用逐个回头看哪个方法会动设备。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

#: 参数类型写这几个字符串之一；只是声明与校验用，不做复杂转换
KINDS = ("str", "int", "float", "bool", "list", "dict", "any")


@dataclass(frozen=True)
class ParamSpec:
    """一个参数的说明。``default`` 不为空表示可以不传。"""

    name: str
    kind: str = "str"
    required: bool = False
    default: Any = None
    doc: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "kind": self.kind, "required": self.required}
        if not self.required:
            data["default"] = self.default
        if self.doc:
            data["doc"] = self.doc
        return data


def required(name: str, kind: str = "str", doc: str = "") -> ParamSpec:
    """必填参数（写起来比 ``ParamSpec(..., required=True)`` 短）。"""
    return ParamSpec(name=name, kind=kind, required=True, doc=doc)


def optional(name: str, kind: str = "str", default: Any = None, doc: str = "") -> ParamSpec:
    """可选参数。"""
    return ParamSpec(name=name, kind=kind, required=False, default=default, doc=doc)


@dataclass(frozen=True)
class MethodSpec:
    """一个方法：名字、实现、参数表、是否写操作。"""

    name: str
    func: Callable[..., Any]
    summary: str = ""
    params: tuple[ParamSpec, ...] = ()
    #: True = 会改变现场或磁盘（写值、启停采集…）；将来做鉴权/只读/审计按这个拦
    mutates: bool = False
    returns: str = ""

    @property
    def allowed_params(self) -> set[str]:
        return {param.name for param in self.params}

    def describe(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "summary": self.summary,
            "mutates": self.mutates,
            "params": [param.to_dict() for param in self.params],
        }
        if self.returns:
            data["returns"] = self.returns
        return data


class MethodRegistry:
    """``方法名 -> MethodSpec``。重名直接报错（静默覆盖会让脚本调到别人的实现）。"""

    def __init__(self) -> None:
        self._specs: dict[str, MethodSpec] = {}

    def add(self, spec: MethodSpec) -> MethodSpec:
        if not spec.name or not spec.name.strip():
            raise ValueError("方法名不能为空")
        if spec.name in self._specs:
            raise ValueError(f"方法名重复：{spec.name}")
        for param in spec.params:
            if param.kind not in KINDS:
                raise ValueError(f"{spec.name} 的参数 {param.name} 类型写错了：{param.kind}")
        self._specs[spec.name] = spec
        return spec

    def method(
        self,
        name: str,
        summary: str = "",
        params: tuple[ParamSpec, ...] = (),
        *,
        mutates: bool = False,
        returns: str = "",
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """装饰器写法：``@registry.method("point.read", "读一个点位", (required("pid"),))``。"""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.add(
                MethodSpec(
                    name=name,
                    func=func,
                    summary=summary,
                    params=tuple(params),
                    mutates=mutates,
                    returns=returns,
                )
            )
            return func

        return decorator

    def get(self, name: str) -> MethodSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[MethodSpec]:
        return [self._specs[name] for name in self.names()]

    def describe(self) -> list[dict[str, Any]]:
        return [spec.describe() for spec in self.specs()]

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return name in self._specs


__all__ = [
    "KINDS",
    "MethodRegistry",
    "MethodSpec",
    "ParamSpec",
    "optional",
    "required",
]
