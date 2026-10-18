"""外部脚本调用示例：只读地看一眼工程，再演示怎么按需写一个值。

用法（不装界面也能跑，`icomm.api` 不依赖 Qt）::

    # 只看：工程名、采集状态、设备丢包率、某个值、最近报警
    python examples/api_script_example.py "C:/工程目录"

    # 明确允许写值（会真的动设备！）
    python examples/api_script_example.py "C:/工程目录" --write 储能变流器PCS.有功功率给定=50

要点：
* ``open_plant()`` **默认只读**，要写值必须显式 ``read_only=False`` + ``--write``；
* 读的是采集线程写进来的缓存值（不发新请求），所以要先有采集在跑；
* 想批量/长期对接，用 ``service.handle_json()`` 或自己实现
  :class:`icomm.api.transport.Transport`（见 icomm/api/transport.py）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icomm.api import ApiError, build_service, open_plant  # noqa: E402


def parse_writes(items: list[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"写值参数要写成 点号=值：{item}")
        pid, _, raw = item.partition("=")
        pairs.append((pid.strip(), raw.strip()))
    return pairs


def coerce(raw: str):
    """就地把 "50" / "1.5" / "true" 变成数字或布尔；其它当字符串（命令点位用）。"""
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    for caster in (int, float):
        try:
            return caster(raw)
        except ValueError:
            continue
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="VVPhmi 外部脚本接口示例")
    parser.add_argument("project", help="工程目录 / project.json / .iprj")
    parser.add_argument("--write", action="append", metavar="点位=值",
                        help="允许写值并给出要写的点位（可重复）；不给就是纯只读")
    parser.add_argument("--pid", default="", help="要单独打印的点位")
    args = parser.parse_args()

    writes = parse_writes(args.write)
    plant = open_plant(args.project, read_only=not writes)
    try:
        service = build_service(plant)
        print("工程：", service.call("project.info").result["name"])
        print("能力：", service.call("api.describe").result["method_count"], "个方法，"
              f"只读={plant.read_only}")

        def call(method: str, params: dict | None = None):
            """调一次并打印错误 —— 脚本里最省事的写法。"""
            response = service.call(method, params or {})
            if not response.ok:
                print(f"  {method} 失败：", response.error["code"], response.error["message"])
                return None
            return response.result

        status = call("engine.status")
        print("采集：", status if status["running"] else "没启动（实时值要采集在跑才有的读）")
        for device in call("device.list") or []:
            print(f"  设备 {device['name']}: {device['state']} 丢包 {device['loss_rate']}")

        if args.pid:
            row = call("point.read", {"pid": args.pid})
            if row:
                print(f"  {row['pid']} = {row['value']} {row['quality']}")

        # 事件订阅：回调在引擎线程里，脚本里自己注意加锁
        seen: list[int] = []
        service.subscribe("tags", lambda values: seen.append(len(values)))
        time.sleep(0.2)
        print("  0.2 秒内收到", len(seen), "批实时值推送")

        for pid, raw in writes:
            value = coerce(raw)
            row = call("point.write", {"pid": pid, "value": value})
            if row:
                print(f"  写入 {pid} = {value} → {row['value']}")

        alarms = call("alarm.list", {"limit": 5, "only_active": True}) or []
        print("  未复位报警：", len(alarms), "条")
        return 0
    except ApiError as error:
        print("接口报错：", error.code, error.message, file=sys.stderr)
        return 2
    finally:
        plant.close()


if __name__ == "__main__":
    raise SystemExit(main())
