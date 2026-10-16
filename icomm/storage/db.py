"""SQLite 存储。

设计要点：
* 写入集中到一个后台线程，采集线程只往队列里塞，绝不阻塞采集周期；
* 查询用独立的短连接，避免跨线程共享 sqlite3 连接；
* 开启 WAL，读写并发更稳。
"""

from __future__ import annotations

import array
import os
import queue
import sqlite3
import threading
import time
from contextlib import closing
from typing import Any, Iterable

#: 攒够多少条就提交一次
COMMIT_BATCH = 200
#: 最长多久必须提交一次。持续采集时队列几乎不会空闲，
#: 只靠「攒批」提交会让数据长时间停留在未提交事务里，
#: 既查不到（别的连接读不到）也可能在崩溃时丢失。
COMMIT_INTERVAL_S = 1.0

_SENTINEL = object()

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    ts    REAL NOT NULL,
    pid   TEXT NOT NULL,
    value REAL
);
CREATE INDEX IF NOT EXISTS idx_history_pid_ts ON history (pid, ts);

CREATE TABLE IF NOT EXISTS alarms (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL,
    pid     TEXT,
    device  TEXT,
    tag     TEXT,
    level   TEXT,
    message TEXT,
    value   TEXT,
    active  INTEGER DEFAULT 1,
    acked   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alarms_ts ON alarms (ts);

CREATE TABLE IF NOT EXISTS comm_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL,
    device  TEXT,
    level   TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_comm_log_ts ON comm_log (ts);

-- 录波片段：一段数组/块点位读回来的波形。
-- 用 float32 二进制并存，比按点一行存 SQLite 紧凑得多
-- （200 点一段 = 800 字节，若按行存要 200 行 + 索引开销）。
CREATE TABLE IF NOT EXISTS waveforms (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    pid     TEXT NOT NULL,
    n       INTEGER NOT NULL,
    dt_us   INTEGER DEFAULT 0,
    data    BLOB
);
CREATE INDEX IF NOT EXISTS idx_waveforms_pid_ts ON waveforms (pid, ts);
"""


class Storage:
    """归档与日志写入器。"""

    def __init__(self, path: str = "data/icomm.db", queue_size: int = 20000) -> None:
        self.path = path
        self._queue: queue.Queue[tuple[str, tuple] | None] = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._dropped = 0
        self._last_error = ""

    # ------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._thread is not None:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)

        conn = sqlite3.connect(self.path)
        try:
            conn.executescript("PRAGMA journal_mode=WAL;")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vvphmi-storage", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        try:
            self._queue.put_nowait(_SENTINEL)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)
        # 只有线程真的退出了才认为"停了"：否则库文件还开着，
        # 说成已停止会让人以为可以安全删/移这个库（Windows 上删不掉）。
        # 留着引用，调用方可以再 stop() 一次。
        if not self._thread.is_alive():
            self._thread = None

    @property
    def stopped(self) -> bool:
        """归档线程是否真的已经退出（库文件已经可以删/移）。"""
        return self._thread is None or not self._thread.is_alive()

    @property
    def dropped(self) -> int:
        """因队列满或写入失败被丢弃的记录数。"""
        return self._dropped

    @property
    def last_error(self) -> str:
        """最近一次写入错误，便于现场排查归档为何缺数据。"""
        return self._last_error

    # ------------------------------------------------------ 写入接口

    def archive(self, pid: str, value: float, ts: float | None = None) -> None:
        self._submit("history", (ts or time.time(), pid, _as_float(value)))

    def add_alarm(
        self,
        pid: str,
        device: str,
        tag: str,
        level: str,
        message: str,
        value: Any = None,
        active: bool = True,
        acked: bool = False,
        ts: float | None = None,
    ) -> None:
        self._submit(
            "alarm",
            (
                ts or time.time(),
                pid,
                device,
                tag,
                level,
                message,
                "" if value is None else str(value),
                1 if active else 0,
                1 if acked else 0,
            ),
        )

    def log(self, device: str, level: str, message: str, ts: float | None = None) -> None:
        self._submit("log", (ts or time.time(), device, level, message))

    def add_waveform(
        self,
        pid: str,
        values: Iterable[float],
        dt_us: int = 0,
        ts: float | None = None,
    ) -> None:
        """保存一段录波。``dt_us`` 是相邻采样点间隔（微秒），0 表示未知。"""
        items = list(values)
        if not items:
            return
        buffer = array.array("f", (float(v) for v in items))
        self._submit(
            "waveform", (ts or time.time(), pid, len(items), int(dt_us), buffer.tobytes())
        )

    def _submit(self, kind: str, payload: tuple) -> None:
        try:
            self._queue.put_nowait((kind, payload))
        except queue.Full:
            self._dropped += 1

    # ------------------------------------------------------ 写入线程

    def _run(self) -> None:
        conn = sqlite3.connect(self.path)
        conn.executescript("PRAGMA journal_mode=WAL;")
        pending = 0
        last_commit = time.monotonic()

        def commit() -> None:
            nonlocal pending, last_commit
            if pending:
                conn.commit()
                pending = 0
            last_commit = time.monotonic()

        try:
            while True:
                try:
                    item = self._queue.get(timeout=COMMIT_INTERVAL_S)
                except queue.Empty:
                    item = None  # 仅表示超时，用于触发定期提交

                if item is _SENTINEL:
                    break
                if item is None:
                    commit()
                    if self._stop.is_set() and self._queue.empty():
                        break
                    continue

                kind, payload = item
                try:
                    if kind == "history":
                        conn.execute("INSERT INTO history (ts, pid, value) VALUES (?,?,?)", payload)
                    elif kind == "alarm":
                        conn.execute(
                            "INSERT INTO alarms (ts, pid, device, tag, level, message, value, active, acked)"
                            " VALUES (?,?,?,?,?,?,?,?,?)",
                            payload,
                        )
                    elif kind == "log":
                        conn.execute(
                            "INSERT INTO comm_log (ts, device, level, message) VALUES (?,?,?,?)", payload
                        )
                    elif kind == "waveform":
                        conn.execute(
                            "INSERT INTO waveforms (ts, pid, n, dt_us, data) VALUES (?,?,?,?,?)", payload
                        )
                    pending += 1
                except Exception as exc:
                    # 不能让一条坏记录终止整个归档线程，但也不能静默丢弃
                    self._dropped += 1
                    self._last_error = f"{kind}: {type(exc).__name__}: {exc}"

                if pending >= COMMIT_BATCH or (time.monotonic() - last_commit) >= COMMIT_INTERVAL_S:
                    commit()
            commit()
        finally:
            conn.close()

    # ------------------------------------------------------ 查询接口

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def query_history(
        self,
        pids: Iterable[str],
        start_ts: float,
        end_ts: float,
        limit: int = 20000,
    ) -> dict[str, list[tuple[float, float]]]:
        """返回 ``{点号: [(时间戳, 值), ...]}``。"""
        pids = list(pids)
        result: dict[str, list[tuple[float, float]]] = {p: [] for p in pids}
        if not pids:
            return result

        placeholders = ",".join("?" for _ in pids)
        sql = (
            f"SELECT ts, pid, value FROM history WHERE pid IN ({placeholders})"
            " AND ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT ?"
        )
        # 注意：sqlite3.Connection 作为上下文管理器只提交事务、不关闭连接，
        # 必须用 closing 包一层，否则每次查询都会泄漏一个文件句柄
        # （Windows 上会导致数据库文件无法删除/重命名）。
        with closing(self._connect()) as conn:
            for row in conn.execute(sql, (*pids, start_ts, end_ts, limit)):
                result.setdefault(row["pid"], []).append((row["ts"], row["value"]))
        return result

    def query_alarms(self, limit: int = 500, only_active: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM alarms"
        params: list[Any] = []
        if only_active:
            sql += " WHERE active = 1"
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def query_logs(self, limit: int = 500, device: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM comm_log"
        params: list[Any] = []
        if device:
            sql += " WHERE device = ?"
            params.append(device)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def history_pids(self) -> list[str]:
        with closing(self._connect()) as conn:
            return [r["pid"] for r in conn.execute("SELECT DISTINCT pid FROM history ORDER BY pid")]

    # ------------------------------------------------------ 录波

    def query_waveform_index(self, pid: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """录波清单（只取元信息，不带数据本体，避免一次读回大量 BLOB）。"""
        sql = "SELECT id, ts, pid, n, dt_us FROM waveforms"
        params: list[Any] = []
        if pid:
            sql += " WHERE pid = ?"
            params.append(pid)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def query_waveform(self, waveform_id: int) -> list[float]:
        """按 id 取回一段录波数据（反序列化为 float 列表）。"""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT data FROM waveforms WHERE id = ?", (waveform_id,)
            ).fetchone()
        if row is None or row["data"] is None:
            return []
        buffer = array.array("f")
        buffer.frombytes(row["data"])
        return buffer.tolist()

    def waveform_pids(self) -> list[str]:
        with closing(self._connect()) as conn:
            return [
                r["pid"]
                for r in conn.execute("SELECT DISTINCT pid FROM waveforms ORDER BY pid")
            ]

    def stats(self) -> dict[str, int]:
        with closing(self._connect()) as conn:
            return {
                "history": conn.execute("SELECT COUNT(*) FROM history").fetchone()[0],
                "alarms": conn.execute("SELECT COUNT(*) FROM alarms").fetchone()[0],
                "logs": conn.execute("SELECT COUNT(*) FROM comm_log").fetchone()[0],
                "waveforms": conn.execute("SELECT COUNT(*) FROM waveforms").fetchone()[0],
            }

    def purge(self, retention_days: int) -> int:
        """删除超期数据，返回删除行数。"""
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        removed = 0
        with closing(self._connect()) as conn:
            for table in ("history", "alarms", "comm_log", "waveforms"):
                cur = conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                removed += cur.rowcount or 0
            conn.commit()
        return removed


def _as_float(value: Any) -> float | None:
    """历史表统一按数值存；布尔存 0/1。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
