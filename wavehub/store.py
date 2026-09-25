"""事件存储与扫描去重。

SQLite 单文件持久化，所有业务事实都是只追加事件：
- event_log:     事件流，seq 单调递增，按 aggregate 维护版本号；
- scan_dedup:    scan_id 唯一，重复扫描不再产生第二遍效果（离线补传幂等）；
- seal_record:   每个 delivery_version 仅允许一条封签成功记录，
                 并发封签由唯一约束兜底，只留一个结果。

进程崩溃后用 replay() 重放事件即可恢复全部状态。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA = """
create table if not exists event_log (
    seq           integer primary key autoincrement,
    event_id      text not null unique,
    event_type    text not null,
    aggregate_id  text not null,
    aggregate_ver integer not null,
    occurred_at   text not null,
    recorded_at   text not null,
    delivery_version text,
    payload       text not null
);
create table if not exists scan_dedup (
    scan_id      text primary key,
    event_type   text not null,
    aggregate_id text not null,
    first_seen   text not null
);
create table if not exists seal_record (
    delivery_version text primary key,
    wave_id          text not null,
    vehicle_id       text not null,
    driver_id        text not null,
    seal_event_id    text not null,
    sealed_at        text not null
);
create table if not exists idempotency (
    command_key text primary key,
    result      text not null
);
"""


class ConcurrentSealError(Exception):
    """唯一约束冲突：同一配送版本已经封签。"""


class EventStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma foreign_keys = on")
        self.conn.execute("pragma busy_timeout = 30000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- 读取 ----------

    def replay(self) -> Iterator[sqlite3.Row]:
        """按写入顺序（事实发生的接受顺序）重放全部事件。

        离线补传的乱序事件按 occurred_at 业务时间由投影解释；
        seq 只保证追加顺序，投影据此完成“以业务发生时间校验”。
        """
        yield from self.conn.execute(
            "select * from event_log order by seq"
        )

    def events_of(self, aggregate_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "select * from event_log where aggregate_id = ? order by seq",
            (aggregate_id,),
        ))

    def next_version(self, aggregate_id: str) -> int:
        row = self.conn.execute(
            "select coalesce(max(aggregate_ver), 0) as v from event_log where aggregate_id = ?",
            (aggregate_id,),
        ).fetchone()
        return int(row["v"]) + 1

    def seen_scan(self, scan_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "select * from scan_dedup where scan_id = ?", (scan_id,)
        ).fetchone()

    # ---------- 写入 ----------

    def append(
        self,
        *,
        event_id: str,
        event_type: str,
        aggregate_id: str,
        occurred_at: str,
        recorded_at: str,
        payload: dict[str, Any],
        delivery_version: str | None = None,
    ) -> sqlite3.Row:
        ver = self.next_version(aggregate_id)
        try:
            cur = self.conn.execute(
                "insert into event_log(event_id, event_type, aggregate_id, aggregate_ver, "
                "occurred_at, recorded_at, delivery_version, payload) values (?,?,?,?,?,?,?,?)",
                (
                    event_id, event_type, aggregate_id, ver,
                    occurred_at, recorded_at, delivery_version,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
        except sqlite3.IntegrityError as exc:  # event_id 重复
            raise ValueError(f"事件标识重复：{event_id}") from exc
        return self.conn.execute(
            "select * from event_log where seq = ?", (cur.lastrowid,)
        ).fetchone()

    def remember_scan(self, scan_id: str, event_type: str, aggregate_id: str, when: str) -> None:
        self.conn.execute(
            "insert or ignore into scan_dedup(scan_id, event_type, aggregate_id, first_seen) "
            "values (?,?,?,?)",
            (scan_id, event_type, aggregate_id, when),
        )

    def try_seal(
        self, delivery_version: str, wave_id: str, vehicle_id: str,
        driver_id: str, seal_event_id: str, sealed_at: str,
    ) -> bool:
        """原子抢占封签权。返回 False 表示该配送版本已被他人封签。"""
        try:
            self.conn.execute(
                "insert into seal_record(delivery_version, wave_id, vehicle_id, driver_id, "
                "seal_event_id, sealed_at) values (?,?,?,?,?,?)",
                (delivery_version, wave_id, vehicle_id, driver_id, seal_event_id, sealed_at),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def seal_of(self, delivery_version: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "select * from seal_record where delivery_version = ?", (delivery_version,)
        ).fetchone()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # ---------- 诊断 ----------

    def all_events(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("select * from event_log order by seq"))
