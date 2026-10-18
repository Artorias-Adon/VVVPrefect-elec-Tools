"""标准方法集：把 :class:`~icomm.api.Plant` 的能力登记成可远程调用的方法。

方法名按"名词.动词"分组，稳定不改（脚本会照着这些名字写）：

=================  ================================================  ======
方法                作用                                              写操作
=================  ================================================  ======
api.ping           只回 ``pong``，用来测通道通不通                   否
api.describe       能力清单：版本、方法、参数、事件、只读状态        否
project.info       工程名、设备/通道/点位数量、工程目录              否
engine.start       开始采集                                          是
engine.stop        停止采集                                          是
engine.status      采集在不在跑、在线设备数                          否
device.list        每台设备的通信状态与丢包率                        否
point.list         点位元数据（可按摩设备名/是否可写过滤）           否
point.read         读一个点位的实时缓存值                            否
point.read_many    批量读（返回 ``{点号: 值}``）                      否
point.snapshot     一片点位的完整实时信息（质量戳、时间戳）          否
point.waveform     数组点位的最近一段波形                            否
point.write        写一个点位（会真的动设备）                        是
history.query      历史曲线                                          否
alarm.list         报警记录（可只看未复位的）                        否
log.query          通信日志                                          否
storage.stats      历史库条数                                        否
=================  ================================================  ======

往这里加方法时**三件事一起做**：实现、``params`` 说明、``mutates`` 标对。
``mutates`` 标错会让只读模式漏拦，那是会动设备的。
"""

from __future__ import annotations

from .envelope import API_VERSION
from .plant import Plant
from .service import ApiService
from .registry import optional, required


def build_service(plant: Plant, *, read_only: bool | None = None) -> ApiService:
    """给一个 Plant 配出**带标准方法**的服务，并把 Plant 的事件接到事件总线上。"""
    service = ApiService(plant, read_only=read_only)
    _register(service, plant)
    plant.listen(service.publish)
    return service


def _register(service: ApiService, plant: Plant) -> None:
    # -------------------------------------------------- 自检
    service.add(
        "api.ping",
        lambda: {"pong": True, "api_version": API_VERSION},
        "测通道：成功就回 pong",
    )
    service.add(
        "api.describe",
        service.manifest,
        "能力清单：协议版本、所有方法及参数、事件名、只读状态",
        returns="{api_version, methods[], events[], read_only, project, engine_running}",
    )

    # -------------------------------------------------- 工程 / 采集
    service.add(
        "project.info",
        lambda: {
            "name": plant.project.name,
            "description": getattr(plant.project, "description", ""),
            "devices": [device.name for device in plant.project.devices],
            "channels": [channel.name for channel in plant.project.channels],
            "points": len(plant.project.all_point_ids()),
            "root": getattr(plant.paths, "root", ""),
            "project_file": getattr(plant.paths, "project_file", ""),
            "db_file": getattr(plant.paths, "db_file", ""),
            "read_only": plant.read_only,
        },
        "工程概况：设备/通道/点位清单与目录位置",
    )
    service.add(
        "engine.start",
        plant.start,
        "开始采集（脚本用法下会自动建引擎）",
        mutates=True,
        returns="{running, online, devices, ...}",
    )
    service.add(
        "engine.stop",
        plant.stop,
        "停止采集（持续发送一并停）",
        mutates=True,
        returns="{running, online, devices, ...}",
    )
    service.add("engine.status", plant.engine_status, "采集状态与在线设备数")
    service.add("device.list", plant.device_statuses, "每台设备的通信状态与丢包率")

    # -------------------------------------------------- 点位
    service.add(
        "point.list",
        plant.points,
        "点位元数据（地址、类型、单位、限幅、报警阈值…）",
        (
            optional("device", "str", "", "只看这台设备"),
            optional("writable", "bool", None, "只看可写（true）或只读（false）"),
        ),
        returns="[{pid, title, unit, data_type, writable, write_min, write_max, alarm, ...}]",
    )
    service.add(
        "point.read",
        plant.read,
        "读一个点位的实时缓存值（不发请求，是采集线程刚写进来的那份）",
        (required("pid", "str", "点号：设备名.点名"),),
        returns="{pid, value, quality, timestamp, age_s, good}",
    )
    service.add(
        "point.read_many",
        lambda pids, only_good=False: {
            pid: row["value"] for pid, row in plant.snapshot(only_good=only_good).items()
            if pid in set(pids)
        },
        "批量读值：返回 {点号: 值}",
        (
            required("pids", "list", "点号列表"),
            optional("only_good", "bool", False, "只返回质量良好的"),
        ),
    )
    service.add(
        "point.snapshot",
        plant.snapshot,
        "一片点位的完整实时信息（含质量戳）",
        (
            optional("device", "str", "", "只看这台设备"),
            optional("only_good", "bool", False, "只返回质量良好的"),
        ),
    )
    service.add(
        "point.waveform",
        plant.waveform,
        "数组/块点位的最近一段波形",
        (required("pid", "str", "数组点位的点号"),),
        returns="[float, ...]",
    )
    service.add(
        "point.write",
        plant.write,
        "写一个点位（会真的动设备；只读模式下拒绝）",
        (
            required("pid", "str", "点号"),
            required("value", "any", "要写的值：数值、true/false，命令点位写字面量"),
            optional("timeout", "float", None, "等设备回应的秒数，默认用软件设置"),
        ),
        mutates=True,
        returns="{pid, value, quality, timestamp}",
    )

    # -------------------------------------------------- 历史 / 报警 / 日志
    service.add(
        "history.query",
        plant.history,
        "查历史曲线；时间是 Unix 秒，不给就取最近一小时",
        (
            required("pids", "list", "点号列表"),
            optional("start", "float", None, "起始时间（Unix 秒）"),
            optional("end", "float", None, "结束时间（Unix 秒）"),
            optional("limit", "int", 20000, "最多取多少条"),
        ),
        returns="{点号: [[时间戳, 值], ...]}",
    )
    service.add(
        "alarm.list",
        plant.alarms,
        "报警记录",
        (
            optional("limit", "int", 500, "最多多少条"),
            optional("only_active", "bool", False, "只看还没复位的"),
        ),
    )
    service.add(
        "log.query",
        plant.logs,
        "通信日志",
        (
            optional("limit", "int", 500, "最多多少条"),
            optional("device", "str", "", "只看某台设备（按通道名过滤）"),
        ),
    )
    service.add("storage.stats", plant.storage_stats, "历史库条数统计")


def method_names(plant: Plant) -> list[str]:
    """标准方法名清单（按字典序）。传一个 Plant 即可，不用启动采集。"""
    return build_service(plant).registry.names()


__all__ = ["build_service", "method_names"]
