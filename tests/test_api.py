"""外部脚本接口：信封、方法表、门面、事件、只读开关与界面接线。

这些测试盯的是"**以后开外部接口时不踩雷**"：

* 信封能来回转、版本高了明确说 `unsupported`（而不是瞎解释参数）；
* 参数校验真的拦得住（缺参数、类型不对、多传参数都报错）；
* 只读模式下**写值一律拒绝**（写值会真的动设备，这是安全底线）；
* 引擎的失败文案能翻成稳定的错误码，脚本按码判断而不是按中文猜；
* 事件转发**不抢**界面原有的回调；
* ``icomm.api`` 不把 Qt 拖进来（脚本/服务端不该被迫装 GUI 依赖）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from icomm.api import (
    API_VERSION,
    BAD_REQUEST,
    DENIED,
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
    ApiService,
    DirectTransport,
    MethodRegistry,
    MethodSpec,
    ParamSpec,
    Plant,
    build_service,
    open_plant,
    optional,
    required,
)
from icomm.core import project as project_io
from icomm.core import project_store
from icomm.core.tags import TagTable
from icomm.storage import Storage

PID = "储能变流器PCS.有功功率给定"


# --------------------------------------------------------------- 测试用引擎


class FakeEngine:
    """够用的假引擎：真标签表、假写值结果，不碰网络。"""

    def __init__(self, project, *, running=True) -> None:
        self.project = project
        self.tags = TagTable()
        for pid in project.all_point_ids():
            self.tags.ensure(pid)
        self.running = running
        self.statuses: dict = {}
        self.on_tags = None
        self.on_device_state = None
        self.on_alarm = None
        self.on_frame = None
        self.on_log = None
        self.write_result: tuple[bool, str] = (True, "")
        self.writes: list[tuple[str, object]] = []

    # 引擎接口
    def restart(self, project=None) -> None:
        if project is not None:
            self.project = project
        self.running = True

    def stop(self) -> None:
        self.running = False

    def online_count(self) -> tuple[int, int]:
        return (1, len(self.project.devices))

    def device_summary(self) -> list:
        return []

    def write(self, pid, value, timeout=None):
        self.writes.append((pid, value))
        if self.write_result[0]:
            self.tags.update(pid, value)
        return self.write_result


def wait_flush(storage: Storage, want: int, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    stats = storage.stats()
    while time.time() < deadline and stats["history"] < want:
        time.sleep(0.05)
        stats = storage.stats()
    return stats


# --------------------------------------------------------------- 信封


class EnvelopeTest(unittest.TestCase):
    def test_request_defaults(self) -> None:
        request = ApiRequest.from_dict({"method": "point.read"})
        self.assertEqual(request.method, "point.read")
        self.assertEqual(request.params, {})
        self.assertEqual(request.version, API_VERSION)
        self.assertEqual(request.id, "")

    def test_request_roundtrip(self) -> None:
        original = ApiRequest(method="point.write", params={"pid": PID, "value": 1}, id="7")
        again = ApiRequest.from_dict(original.to_dict())
        self.assertEqual(again, original)

    def test_request_rejects_bad_shapes(self) -> None:
        cases = [
            ("不是对象", "请求必须是一个对象"),
            ({}, "缺少 method"),
            ({"method": "  "}, "缺少 method"),
            ({"method": "x", "params": [1]}, "params 必须是对象"),
            ({"method": "x", "version": "1"}, "version 必须是整数"),
            ({"method": "x", "id": {}}, "id 只能是字符串或整数"),
        ]
        for payload, hint in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ApiError) as caught:
                    ApiRequest.from_dict(payload)
                self.assertEqual(caught.exception.code, BAD_REQUEST)
                self.assertIn(hint, caught.exception.message)

    def test_newer_version_is_unsupported(self) -> None:
        with self.assertRaises(ApiError) as caught:
            ApiRequest.from_dict({"method": "api.ping", "version": API_VERSION + 1})
        self.assertEqual(caught.exception.code, UNSUPPORTED)
        self.assertEqual(caught.exception.detail["server_version"], API_VERSION)

    def test_response_shapes(self) -> None:
        request = ApiRequest(method="api.ping", id="42")
        ok = ApiResponse.success(request, {"pong": True})
        self.assertTrue(ok.ok)
        self.assertEqual(ok.code, OK)
        self.assertEqual(ok.to_dict()["id"], "42")
        self.assertNotIn("error", ok.to_dict())

        bad = ApiResponse.failure(request, ApiError(NOT_FOUND, "没这个点位"))
        self.assertFalse(bad.ok)
        self.assertEqual(bad.code, NOT_FOUND)
        self.assertEqual(bad.to_dict()["error"]["message"], "没这个点位")
        self.assertNotIn("result", bad.to_dict())

        self.assertEqual(ApiResponse.from_dict(ok.to_dict()).result, {"pong": True})

    def test_error_detail_is_optional(self) -> None:
        self.assertEqual(ApiError(INTERNAL, "炸了").to_dict(),
                         {"code": INTERNAL, "message": "炸了"})


# --------------------------------------------------------------- 注册表


class RegistryTest(unittest.TestCase):
    def test_duplicate_name_is_rejected(self) -> None:
        registry = MethodRegistry()
        spec = MethodSpec(name="a.b", func=lambda: 1)
        registry.add(spec)
        with self.assertRaises(ValueError):
            registry.add(MethodSpec(name="a.b", func=lambda: 2))

    def test_bad_kind_is_rejected(self) -> None:
        registry = MethodRegistry()
        with self.assertRaises(ValueError):
            registry.add(MethodSpec(name="a.b", func=lambda: 1,
                                    params=(ParamSpec(name="x", kind="整型"),)))

    def test_decorator_style(self) -> None:
        registry = MethodRegistry()

        @registry.method("math.double", "翻倍", (required("value", "int"),))
        def double(value: int) -> int:
            return value * 2

        self.assertEqual(registry.get("math.double").func(3), 6)
        self.assertIn("math.double", registry)
        self.assertEqual(len(registry), 1)

    def test_describe_keeps_metadata(self) -> None:
        registry = MethodRegistry()
        registry.add(
            MethodSpec(
                name="point.write",
                func=lambda pid, value=None: None,
                summary="写值",
                params=(required("pid"), optional("value", "any", None, "值")),
                mutates=True,
                returns="{ok}",
            )
        )
        described = registry.describe()[0]
        self.assertTrue(described["mutates"], "写操作必须标出来，只读模式靠它拦")
        self.assertEqual([param["name"] for param in described["params"]], ["pid", "value"])
        self.assertTrue(described["params"][0]["required"])
        self.assertIn("default", described["params"][1])
        self.assertEqual(described["returns"], "{ok}")


# --------------------------------------------------------------- 服务


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ApiService(None)

        @self.service.method("math.add", "相加", (required("a", "int"), required("b", "int")))
        def add(a: int, b: int) -> int:
            return a + b

        @self.service.method("math.boom", "抛错")
        def boom() -> None:
            raise RuntimeError("内部炸了")

        @self.service.method("math.deny", "业务拒绝")
        def deny() -> None:
            raise ApiError(NOT_READY, "采集没启动")

        @self.service.method("danger.write", "写点东西", mutates=True)
        def write_something() -> str:
            return "写了"

    def test_success_and_id_echo(self) -> None:
        response = self.service.call("math.add", {"a": 2, "b": 3}, request_id="abc")
        self.assertTrue(response.ok)
        self.assertEqual(response.result, 5)
        self.assertEqual(response.id, "abc")

    def test_unknown_method_lists_known(self) -> None:
        response = self.service.call("nope")
        self.assertEqual(response.code, UNKNOWN_METHOD)
        self.assertIn("math.add", response.error["detail"]["known"])

    def test_missing_required_param(self) -> None:
        response = self.service.call("math.add", {"a": 1})
        self.assertEqual(response.code, BAD_REQUEST)
        self.assertIn("b", response.error["message"])

    def test_unknown_param_is_rejected(self) -> None:
        response = self.service.call("math.add", {"a": 1, "b": 2, "c": 3})
        self.assertEqual(response.code, BAD_REQUEST)
        self.assertIn("c", response.error["message"])

    def test_wrong_type_is_rejected(self) -> None:
        response = self.service.call("math.add", {"a": "1", "b": 2})
        self.assertEqual(response.code, BAD_REQUEST)
        self.assertIn("int", response.error["message"])

    def test_bool_is_not_int(self) -> None:
        response = self.service.call("math.add", {"a": True, "b": 2})
        self.assertEqual(response.code, BAD_REQUEST)

    def test_float_accepts_int(self) -> None:
        service = ApiService(None)
        service.add("math.half", lambda value: value / 2, "减半", (required("value", "float"),))
        self.assertEqual(service.call("math.half", {"value": 3}).result, 1.5)

    def test_handler_exception_becomes_internal(self) -> None:
        response = self.service.call("math.boom")
        self.assertEqual(response.code, INTERNAL)
        self.assertIn("RuntimeError", response.error["message"])
        self.assertIn("traceback", response.error["detail"])
        self.assertTrue(self.service.last_error)

    def test_api_error_passes_through(self) -> None:
        response = self.service.call("math.deny")
        self.assertEqual(response.code, NOT_READY)
        self.assertEqual(response.error["message"], "采集没启动")

    def test_read_only_blocks_mutations(self) -> None:
        service = ApiService(None, read_only=True)
        service.add("danger.write", lambda: "写了", "写", mutates=True)
        service.add("safe.read", lambda: "读了", "读")
        self.assertEqual(service.call("danger.write").code, DENIED)
        self.assertEqual(service.call("safe.read").result, "读了")

    def test_handle_dict_and_json(self) -> None:
        payload = {"id": "1", "method": "math.add", "params": {"a": 1, "b": 1}}
        self.assertEqual(self.service.handle_dict(payload)["result"], 2)

        reply = json.loads(self.service.handle_json(json.dumps(payload)))
        self.assertEqual(reply["result"], 2)

        broken = json.loads(self.service.handle_json("{不是 json"))
        self.assertEqual(broken["error"]["code"], BAD_REQUEST)

        not_object = self.service.handle_dict([1, 2])
        self.assertEqual(not_object["error"]["code"], BAD_REQUEST)

    def test_manifest_shape(self) -> None:
        manifest = self.service.manifest()
        self.assertEqual(manifest["api_version"], API_VERSION)
        self.assertFalse(manifest["read_only"])
        self.assertIn("math.add", [item["name"] for item in manifest["methods"]])
        self.assertIn("tags", manifest["events"])
        self.assertEqual(manifest["project"]["name"], "")

    def test_events_subscribe_publish_unsubscribe(self) -> None:
        seen: list[object] = []
        unsubscribe = self.service.subscribe("tags", seen.append)
        self.assertEqual(self.service.publish("tags", {"a": 1}), 1)
        self.assertEqual(seen, [{"a": 1}])
        unsubscribe()
        self.assertEqual(self.service.publish("tags", {"a": 2}), 0)
        self.assertEqual(self.service.subscriber_count("tags"), 0)

    def test_event_name_is_checked(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.service.subscribe("没有这个事件", lambda payload: None)
        self.assertEqual(caught.exception.code, BAD_REQUEST)

    def test_one_bad_subscriber_does_not_block_others(self) -> None:
        delivered: list[object] = []

        def bad(payload):
            raise RuntimeError("订阅者自己坏了")

        self.service.subscribe("tags", bad)
        self.service.subscribe("tags", delivered.append)
        self.assertEqual(self.service.publish("tags", 1), 1)
        self.assertEqual(delivered, [1])
        self.assertIn("RuntimeError", self.service.last_error)


# --------------------------------------------------------------- 门面


class PlantReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = project_io.bess_project()
        self.engine = FakeEngine(self.project)
        self.plant = Plant(self.project, engine=self.engine)

    def test_read_returns_payload(self) -> None:
        self.engine.tags.update(PID, 123.4)
        row = self.plant.read(PID)
        self.assertEqual(row["pid"], PID)
        self.assertEqual(row["value"], 123.4)
        self.assertTrue(row["good"])
        self.assertLess(row["age_s"], 5)

    def test_read_unknown_point(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.plant.read("没有.这个点")
        self.assertEqual(caught.exception.code, NOT_FOUND)

    def test_read_without_engine(self) -> None:
        plant = Plant(self.project)
        with self.assertRaises(ApiError) as caught:
            plant.read(PID)
        self.assertEqual(caught.exception.code, NOT_READY)

    def test_snapshot_filters(self) -> None:
        self.engine.tags.update(PID, 1.0)
        rows = self.plant.snapshot()
        self.assertGreater(len(rows), 1)
        device = PID.split(".")[0]
        only_device = self.plant.snapshot(device=device)
        self.assertTrue(only_device)
        self.assertTrue(all(pid.startswith(device + ".") for pid in only_device))
        good = self.plant.snapshot(only_good=True)
        self.assertLess(len(good), len(rows), "只有刚更新过的那几个是 good")

    def test_points_metadata_and_filters(self) -> None:
        rows = self.plant.points()
        self.assertTrue(rows)
        first = rows[0]
        for field in ("pid", "device", "name", "data_type", "writable", "unit", "alarm"):
            self.assertIn(field, first)
        writable = self.plant.points(writable=True)
        self.assertTrue(writable)
        self.assertTrue(all(row["writable"] for row in writable))
        readonly = self.plant.points(writable=False)
        self.assertTrue(all(not row["writable"] for row in readonly))

    def test_waveform_rejects_scalar(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.plant.waveform("/".join([PID]))
        # 标量点位没有波形（数组点位才给）
        self.assertIn(caught.exception.code, (NOT_FOUND,))

    def test_engine_status_and_devices(self) -> None:
        status = self.plant.engine_status()
        self.assertTrue(status["running"])
        self.assertEqual(status["devices"], len(self.project.devices))
        rows = self.plant.device_statuses()
        self.assertEqual(len(rows), len(self.project.devices))
        self.assertIn("loss_rate", rows[0])


class PlantWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = project_io.bess_project()
        self.engine = FakeEngine(self.project)
        self.plant = Plant(self.project, engine=self.engine)

    def test_write_ok(self) -> None:
        row = self.plant.write(PID, 50.0)
        self.assertEqual(row["value"], 50.0)
        self.assertEqual(self.engine.writes, [(PID, 50.0)])

    def test_write_blocked_in_read_only(self) -> None:
        plant = Plant(self.project, engine=self.engine, read_only=True)
        with self.assertRaises(ApiError) as caught:
            plant.write(PID, 10.0)
        self.assertEqual(caught.exception.code, DENIED)
        self.assertEqual(self.engine.writes, [])

    def test_write_requires_running_engine(self) -> None:
        self.engine.running = False
        with self.assertRaises(ApiError) as caught:
            self.plant.write(PID, 10.0)
        self.assertEqual(caught.exception.code, NOT_READY)

    def test_write_without_engine(self) -> None:
        plant = Plant(self.project)
        with self.assertRaises(ApiError) as caught:
            plant.write(PID, 10.0)
        self.assertEqual(caught.exception.code, NOT_READY)

    def test_engine_failures_map_to_codes(self) -> None:
        cases = [
            ("通道未运行", NOT_READY),
            ("未找到点位 X", NOT_FOUND),
            ("点位 X 不可写", DENIED),
            ("写入超时", TIMEOUT),
            ("设备返回异常码 03", INTERNAL),
        ]
        for message, code in cases:
            with self.subTest(message=message):
                self.engine.write_result = (False, message)
                with self.assertRaises(ApiError) as caught:
                    self.plant.write(PID, 1.0)
                self.assertEqual(caught.exception.code, code, message)


class PlantEventTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = project_io.bess_project()
        self.engine = FakeEngine(self.project)

    def test_callbacks_are_chained_not_stolen(self) -> None:
        seen_by_ui: list[object] = []
        self.engine.on_tags = seen_by_ui.append  # 模拟界面桥接已经挂在上面
        plant = Plant(self.project, engine=self.engine)

        seen_by_api: list[tuple[str, object]] = []
        plant.listen(lambda event, payload: seen_by_api.append((event, payload)))

        self.engine.on_tags({"PCS.Pset": "whatever"})

        self.assertEqual(seen_by_ui, [{"PCS.Pset": "whatever"}], "界面原来的回调还要收到")
        self.assertEqual(seen_by_api[0][0], "tags")
        self.assertEqual(seen_by_api[0][1], {"PCS.Pset": "whatever"})

    def test_attach_twice_does_not_duplicate(self) -> None:
        plant = Plant(self.project, engine=self.engine)
        seen: list[object] = []
        plant.listen(lambda event, payload: seen.append(event))
        plant.attach_engine(self.engine)
        plant.attach_engine(self.engine)
        self.engine.on_tags({})
        self.assertEqual(seen, ["tags"], "同一个引擎重复 attach 不该发两遍")

    def test_service_gets_plant_events(self) -> None:
        plant = Plant(self.project, engine=self.engine)
        service = build_service(plant)
        received: list[object] = []
        service.subscribe("tags", received.append)
        self.engine.on_tags({"a": 1})
        self.assertEqual(received, [{"a": 1}])


# --------------------------------------------------------------- 标准方法集


class StandardMethodsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = project_io.bess_project()
        self.engine = FakeEngine(self.project)
        self.plant = Plant(self.project, engine=self.engine)
        self.service = build_service(self.plant)
        self.api = DirectTransport(self.service)

    def test_expected_methods_exist(self) -> None:
        expected = {
            "api.ping", "api.describe", "project.info", "engine.start", "engine.stop",
            "engine.status", "device.list", "point.list", "point.read", "point.read_many",
            "point.snapshot", "point.waveform", "point.write", "history.query",
            "alarm.list", "log.query", "storage.stats",
        }
        self.assertEqual(set(self.service.registry.names()), expected)

    def test_mutating_methods_are_marked(self) -> None:
        marked = {spec.name for spec in self.service.registry.specs() if spec.mutates}
        self.assertEqual(marked, {"engine.start", "engine.stop", "point.write"},
                         "写操作标错，只读模式就会漏拦")

    def test_ping_and_describe(self) -> None:
        self.assertTrue(self.api.call("api.ping").result["pong"])
        described = self.api.call("api.describe").result
        self.assertEqual(described["api_version"], API_VERSION)
        self.assertEqual(described["project"]["name"], self.project.name)
        self.assertEqual(described["project"]["points"], len(self.project.all_point_ids()))

    def test_project_info(self) -> None:
        info = self.api.call("project.info").result
        self.assertEqual(info["name"], self.project.name)
        self.assertEqual(len(info["devices"]), len(self.project.devices))
        self.assertTrue(info["channels"])

    def test_point_read_many(self) -> None:
        self.engine.tags.update(PID, 7.5)
        values = self.api.call("point.read_many", pids=[PID]).result
        self.assertEqual(values, {PID: 7.5})

    def test_write_through_service(self) -> None:
        response = self.api.call("point.write", pid=PID, value=12.0)
        self.assertTrue(response.ok)
        self.assertEqual(response.result["value"], 12.0)

    def test_read_only_service_refuses_write(self) -> None:
        service = build_service(self.plant, read_only=True)
        response = service.call("point.write", {"pid": PID, "value": 1.0})
        self.assertEqual(response.code, DENIED)
        self.assertEqual(self.engine.writes, [])

    def test_engine_start_stop(self) -> None:
        self.engine.running = False
        self.assertTrue(self.api.call("engine.start").result["running"])
        self.assertFalse(self.api.call("engine.stop").result["running"])

    def test_direct_transport_helpers(self) -> None:
        self.assertTrue(self.api.call_dict("api.ping")["ok"])
        self.assertTrue(self.api.request(ApiRequest(method="api.ping")).ok)
        self.api.shutdown()  # 允许重复调用

    def test_json_roundtrip_through_service(self) -> None:
        text = self.service.handle_json(json.dumps({"method": "point.read", "params": {"pid": PID}}))
        self.assertTrue(json.loads(text)["ok"])


# --------------------------------------------------------------- 打开工程 / 历史


class OpenPlantTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.project = project_io.bess_project()
        self.paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, project_store.safe_name(self.project.name)),
            self.project,
        )
        self.plant = open_plant(self.paths.root)
        self.addCleanup(self.plant.close)

    def test_default_is_read_only(self) -> None:
        self.assertTrue(self.plant.read_only)
        with self.assertRaises(ApiError) as caught:
            self.plant.write(PID, 1.0)
        self.assertIn(caught.exception.code, (DENIED, NOT_READY))

    def test_history_is_opened_lazily(self) -> None:
        self.assertIsNotNone(self.plant.storage, "打开时就把库连上，查历史不用再等")
        rows = self.plant.history([PID], time.time() - 60, time.time() + 60)
        self.assertEqual(rows, {PID: []})

    def test_history_unknown_point(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.plant.history(["没有.这个点"])
        self.assertEqual(caught.exception.code, NOT_FOUND)

    def test_history_empty_pids(self) -> None:
        with self.assertRaises(ApiError) as caught:
            self.plant.history([])
        self.assertEqual(caught.exception.code, BAD_REQUEST)

    def test_history_reads_archived_values(self) -> None:
        now = time.time()
        storage = self.plant.storage
        storage.archive(PID, 1.5, now - 10)
        storage.archive(PID, 2.5, now - 5)
        wait_flush(storage, 2)
        rows = self.plant.history([PID], now - 60, now + 1)
        self.assertEqual([value for _ts, value in rows[PID]], [1.5, 2.5])

    def test_alarms_and_logs_are_lists(self) -> None:
        self.assertIsInstance(self.plant.alarms(limit=5), list)
        self.assertIsInstance(self.plant.logs(limit=5), list)
        self.assertIn("history", self.plant.storage_stats())

    def test_writable_plant_can_write_after_start(self) -> None:
        plant = open_plant(self.paths.root, read_only=False)
        self.addCleanup(plant.close)
        plant.start()  # 真实引擎：通道连不上也照样"在跑"，只是设备报错
        self.assertTrue(plant.running)
        ok = False
        try:
            plant.write(PID, 1.0, timeout=0.2)
            ok = True
        except ApiError as error:
            # 没有真设备时写值当然会失败，但错误码必须是"通道/超时/内部"这类业务码
            self.assertIn(error.code, (NOT_READY, TIMEOUT, INTERNAL))
        self.assertTrue(ok or plant.engine is not None)
        plant.stop()
        self.assertFalse(plant.running)

    def test_context_manager_closes(self) -> None:
        with open_plant(self.paths.root) as plant:
            self.assertIsInstance(plant, Plant)
        self.assertIsNone(plant.storage, "自己开的库要在 with 结束时关掉")


class PlantStartTest(unittest.TestCase):
    def test_read_only_plant_will_not_build_engine(self) -> None:
        project = project_io.bess_project()
        plant = Plant(project, read_only=True)
        with self.assertRaises(ApiError) as caught:
            plant.start()
        self.assertEqual(caught.exception.code, NOT_READY)


# --------------------------------------------------------------- 与界面的接线


class MainWindowWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def test_window_exposes_service(self) -> None:
        from icomm.ui.main_window import MainWindow

        project = project_io.bess_project()
        workdir = os.path.join(self.tmp.name, "work")
        paths = project_store.create_project_dir(
            os.path.join(workdir, project_store.safe_name(project.name)), project
        )
        window = MainWindow(project, paths, workdir=workdir, base_dir=self.tmp.name)
        self.addCleanup(window.close)
        self.addCleanup(window.deleteLater)

        self.assertIsInstance(window.api, ApiService)
        self.assertIs(window.api_plant.project, window.project)
        self.assertIs(window.api_plant.engine, window.engine)
        self.assertIs(window.api_plant.storage, window.storage)
        self.assertIn("point.read", window.api.registry)

        # 工具的上下文里也能拿到（工具/未来的内嵌服务用同一个实例）
        context = window.tool_context()
        self.assertIs(context.services["api"], window.api)

        # 采集没启动时读值也能读到（质量戳是 bad），脚本不用先判状态
        row = window.api.call("point.read", {"pid": PID})
        self.assertTrue(row.ok, row.error)
        self.assertFalse(row.result["good"])

        # 界面这套是"可写"的（跟软件本身一致），只读只是给外部入口的默认值
        self.assertFalse(window.api.read_only)


class NoQtDependencyTest(unittest.TestCase):
    def test_importing_api_does_not_pull_in_qt(self) -> None:
        """脚本/服务端不该因为 import icomm.api 就背上一整套 Qt。"""
        code = (
            "import sys, icomm.api; "
            "print('PySide6' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False", result.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
