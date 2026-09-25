"""门店配送波次中枢：需求合并、截单、波次编排、配送版本、扫描交接与整车转派。

设计要点
--------
- 同一配送版本：司机领单、仓库装箱、车辆封签、门店签收都引用线路的当前
  配送版本；扫描按业务发生时间（occurred_at）定位当时有效的版本来校验。
- 截单语义：截单前需求自动合并；截单后只有获批差异单能改变尚未封签的线路，
  已封签的线路拒绝任何变更，回看记录即可判断改单发生在封签前还是封签后。
- 扫描幂等：scan_id 重复上传直接返回原结果；同一业务（容器×动作×线路）
  不会第二次生效；离线补传按 occurred_at 做容器时序合法性校验。
- 并发封签：active_seal 以 route_id 为主键，任何并发下只保留一个封签结果。
- 持久化：全部状态落 SQLite，恢复运行后待转派车辆与未回收容器仍在正确环节。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from math import ceil

from . import planner
from .db import connect
from .errors import (
    CutoffError,
    InfeasiblePlanError,
    NotFoundError,
    SealConflictError,
    StateError,
    TransferError,
    VersionMismatchError,
)

# 容器交接动作的合法时序：装车 → 签收 → 回收 → 返仓 → 再次装车。
SCAN_TRANSITIONS = {
    "load": {"handover"},
    "handover": {"recover"},
    "recover": {"return"},
    "return": {"load"},
}
FIRST_ACTION = "load"
CONTAINER_STATE_AFTER = {
    "load": "loaded",
    "handover": "handed_over",
    "recover": "return_loaded",
    "return": "at_warehouse",
}
SCAN_EVENT_TYPE = {
    "load": "container.loaded",
    "handover": "container.handed_over",
    "recover": "container.recovered",
    "return": "container.returned",
}


def _parse_ts(value) -> datetime:
    """解析 ISO 8601 时间，必须带时区（领域合同 time_policy）。"""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        raise ValueError("时间必须包含时区（ISO 8601 with timezone）")
    return dt


def _fmt(dt: datetime) -> str:
    return dt.isoformat()


class WaveHub:
    """波次编排与交接系统的门面，所有写操作都在锁与事务内完成。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.conn = connect(db_path)
        self._lock = threading.RLock()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _emit(self, event_type: str, aggregate_id: str, occurred_at: str,
              payload: dict | None = None) -> None:
        self.conn.execute(
            "insert into event_log(event_id, event_type, aggregate_id, occurred_at, payload)"
            " values (?, ?, ?, ?, ?)",
            (f"evt-{uuid.uuid4().hex[:12]}", event_type, aggregate_id, occurred_at,
             json.dumps(payload or {}, ensure_ascii=False)),
        )

    def _row(self, sql: str, params: tuple = ()):
        return self.conn.execute(sql, params).fetchone()

    def _rows(self, sql: str, params: tuple = ()):
        return self.conn.execute(sql, params).fetchall()

    def _must(self, table: str, key: str, value: str):
        row = self._row(f"select * from {table} where {key} = ?", (value,))
        if row is None:
            raise NotFoundError(f"{table} 不存在：{value}")
        return row

    # ------------------------------------------------------------------
    # 主数据
    # ------------------------------------------------------------------
    def add_store(self, store_id: str, name: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("insert into store(store_id, name) values (?, ?)",
                              (store_id, name))

    def set_store_window(self, store_id: str, wave_id: str, *,
                         open_at, close_at) -> None:
        with self._lock, self.conn:
            self._must("store", "store_id", store_id)
            self.conn.execute(
                "insert or replace into store_window(store_id, wave_id, open_at, close_at)"
                " values (?, ?, ?, ?)",
                (store_id, wave_id, _fmt(_parse_ts(open_at)), _fmt(_parse_ts(close_at))),
            )

    def add_product(self, product_id: str, name: str, *,
                    temp_zone: str, coload_group: str, unit_weight: float) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "insert into product(product_id, name, temp_zone, coload_group, unit_weight)"
                " values (?, ?, ?, ?, ?)",
                (product_id, name, temp_zone, coload_group, float(unit_weight)),
            )

    def add_coload_restriction(self, group_a: str, group_b: str, *, reason: str = "") -> None:
        a, b = sorted([group_a, group_b])
        with self._lock, self.conn:
            self.conn.execute(
                "insert or replace into coload_restriction(group_a, group_b, reason)"
                " values (?, ?, ?)",
                (a, b, reason),
            )

    def add_vehicle(self, vehicle_id: str, plate: str, *,
                    capacity: float, temp_zones) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "insert into vehicle(vehicle_id, plate, capacity, temp_zones)"
                " values (?, ?, ?, ?)",
                (vehicle_id, plate, float(capacity), json.dumps(sorted(temp_zones))),
            )

    def add_container(self, container_id: str, kind: str = "turnover_box") -> None:
        with self._lock, self.conn:
            self.conn.execute("insert into container(container_id, kind) values (?, ?)",
                              (container_id, kind))

    def add_recovery_task(self, store_id: str, wave_id: str, empties: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "insert or replace into recovery_task(store_id, wave_id, empties)"
                " values (?, ?, ?)",
                (store_id, wave_id, int(empties)),
            )

    def create_wave(self, wave_id: str, *, cutoff_at) -> None:
        with self._lock, self.conn:
            self.conn.execute("insert into wave(wave_id, cutoff_at) values (?, ?)",
                              (wave_id, _fmt(_parse_ts(cutoff_at))))

    # ------------------------------------------------------------------
    # 需求收集与截单
    # ------------------------------------------------------------------
    def receive_order(self, order_id: str, store_id: str, wave_id: str,
                      lines: list, *, created_at) -> str:
        """截单前接收门店需求；同门店同波次的未截单需求自动合并。

        返回实际承载需求的订单号（发生合并时为被合并的订单号）。
        """
        created = _fmt(_parse_ts(created_at))
        with self._lock, self.conn:
            wave = self._must("wave", "wave_id", wave_id)
            if wave["status"] != "collecting":
                raise CutoffError(f"波次 {wave_id} 已截单，需求只能经由获批差异单变更")
            self._must("store", "store_id", store_id)
            if not lines:
                raise StateError("订单至少需要一行商品")
            for line in lines:
                self._must("product", "product_id", line["product_id"])
                if line["qty"] <= 0:
                    raise StateError("商品数量必须为正数")
            existing = self._row(
                "select order_id from store_order"
                " where store_id = ? and wave_id = ? and status = 'open'",
                (store_id, wave_id),
            )
            if existing is not None:
                target = existing["order_id"]
                for line in lines:
                    self.conn.execute(
                        "insert into order_line(order_id, product_id, qty) values (?, ?, ?)"
                        " on conflict(order_id, product_id)"
                        " do update set qty = qty + excluded.qty",
                        (target, line["product_id"], float(line["qty"])),
                    )
                self.conn.execute(
                    "insert into store_order(order_id, store_id, wave_id, status,"
                    " merged_into, created_at) values (?, ?, ?, 'merged', ?, ?)",
                    (order_id, store_id, wave_id, target, created),
                )
                self._emit("order.merged", target, created, {"merged_order": order_id})
                return target
            self.conn.execute(
                "insert into store_order(order_id, store_id, wave_id, status, created_at)"
                " values (?, ?, ?, 'open', ?)",
                (order_id, store_id, wave_id, created),
            )
            for line in lines:
                self.conn.execute(
                    "insert into order_line(order_id, product_id, qty) values (?, ?, ?)",
                    (order_id, line["product_id"], float(line["qty"])),
                )
            self._emit("order.received", order_id, created,
                       {"store_id": store_id, "wave_id": wave_id})
            return order_id

    def cutoff_wave(self, wave_id: str, *, at) -> None:
        """截单：需求冻结，之后只能经获批差异单变更未封签部分。"""
        at = _fmt(_parse_ts(at))
        with self._lock, self.conn:
            wave = self._must("wave", "wave_id", wave_id)
            if wave["status"] != "collecting":
                raise StateError(f"波次 {wave_id} 当前状态为 {wave['status']}，不能截单")
            self.conn.execute("update wave set status = 'cutoff' where wave_id = ?",
                              (wave_id,))
            self._emit("wave.cutoff", wave_id, at)

    # ------------------------------------------------------------------
    # 波次编排
    # ------------------------------------------------------------------
    def plan_wave(self, wave_id: str, *, depart_at, max_route_minutes: float,
                   service_minutes: float = 10, travel=None,
                   container_capacity: float = 500.0,
                   empty_container_weight: float = 25.0, at=None) -> dict:
        """把截单后的需求编排成装车波次：线路、停靠、首版配送清单与周转箱分配。"""
        depart = _parse_ts(depart_at)
        with self._lock, self.conn:
            wave = self._must("wave", "wave_id", wave_id)
            if wave["status"] != "cutoff":
                raise StateError(f"波次 {wave_id} 当前状态为 {wave['status']}，不能编排")
            planned_at = _fmt(_parse_ts(at)) if at is not None else wave["cutoff_at"]
            demands = [
                planner.DemandLine(
                    order_id=row["order_id"], store_id=row["store_id"],
                    product_id=row["product_id"], qty=row["qty"],
                    weight=row["qty"] * row["unit_weight"],
                    temp_zone=row["temp_zone"], coload_group=row["coload_group"],
                )
                for row in self._rows(
                    "select o.order_id, o.store_id, l.product_id, l.qty,"
                    " p.temp_zone, p.coload_group, p.unit_weight"
                    " from store_order o"
                    " join order_line l on l.order_id = o.order_id"
                    " join product p on p.product_id = l.product_id"
                    " where o.wave_id = ? and o.status = 'open'"
                    " order by o.order_id, l.product_id",
                    (wave_id,),
                )
            ]
            vehicles = [
                planner.VehicleSpec(row["vehicle_id"], row["capacity"],
                                    frozenset(json.loads(row["temp_zones"])))
                for row in self._rows(
                    "select * from vehicle where status = 'available'"
                    " order by vehicle_id")
            ]
            windows = [
                planner.StoreWindow(row["store_id"], _parse_ts(row["open_at"]),
                                    _parse_ts(row["close_at"]))
                for row in self._rows("select * from store_window where wave_id = ?",
                                      (wave_id,))
            ]
            restrictions = {
                (row["group_a"], row["group_b"]): row["reason"]
                for row in self._rows("select * from coload_restriction")
            }
            recovery = {
                row["store_id"]: row["empties"]
                for row in self._rows("select * from recovery_task where wave_id = ?",
                                      (wave_id,))
            }
            container_ids = [
                row["container_id"]
                for row in self._rows(
                    "select container_id from container where status = 'at_warehouse'"
                    " order by container_id")
            ]
            plan = planner.build_plan(
                wave_id=wave_id, demands=demands, vehicles=vehicles, windows=windows,
                restrictions=restrictions, recovery=recovery,
                container_ids=container_ids, container_capacity=container_capacity,
                empty_container_weight=empty_container_weight, depart_at=depart,
                max_route_minutes=max_route_minutes, service_minutes=service_minutes,
                travel=travel,
            )
            for seq, route in enumerate(plan.routes, start=1):
                self.conn.execute(
                    "insert into route(route_id, wave_id, vehicle_id, seq_no, status,"
                    " current_version, created_at) values (?, ?, ?, ?, 'planned', 1, ?)",
                    (route.route_id, wave_id, route.vehicle.vehicle_id, seq, planned_at),
                )
                self.conn.execute(
                    "insert into delivery_version(route_id, version_no, cause, status,"
                    " created_at) values (?, 1, 'cutoff_plan', 'published', ?)",
                    (route.route_id, planned_at),
                )
                for stop_seq, stop in enumerate(route.stops, start=1):
                    self.conn.execute(
                        "insert into route_stop(route_id, seq, store_id, eta)"
                        " values (?, ?, ?, ?)",
                        (route.route_id, stop_seq, stop.store_id, _fmt(stop.eta)),
                    )
                    for line in stop.lines:
                        self.conn.execute(
                            "insert into version_line(route_id, version_no, store_id,"
                            " order_id, product_id, qty) values (?, 1, ?, ?, ?, ?)",
                            (route.route_id, stop.store_id, line.order_id,
                             line.product_id, line.qty),
                        )
            for (route_id, store_id), ids in plan.assignments.items():
                for container_id in ids:
                    self.conn.execute(
                        "insert into container_assignment(route_id, version_no,"
                        " container_id, store_id) values (?, 1, ?, ?)",
                        (route_id, container_id, store_id),
                    )
            for split in plan.splits:
                self.conn.execute(
                    "insert into order_exception(order_id, route_id, store_id, type,"
                    " detail, created_at) values (?, ?, ?, 'split', ?, ?)",
                    (split["order_id"], split["route_ids"][-1],
                     self._row("select store_id from store_order where order_id = ?",
                               (split["order_id"],))["store_id"],
                     f"因[{split['reason']}]拆分到线路 "
                     + "、".join(split["route_ids"]), planned_at),
                )
            self.conn.execute(
                "update store_order set status = 'locked'"
                " where wave_id = ? and status = 'open'",
                (wave_id,),
            )
            self.conn.execute(
                "update wave set status = 'planned', depart_at = ?, max_route_minutes = ?,"
                " service_minutes = ?, container_capacity = ? where wave_id = ?",
                (_fmt(depart), int(max_route_minutes), int(service_minutes),
                 float(container_capacity), wave_id),
            )
            self._emit("wave.planned", wave_id, planned_at,
                       {"routes": [r.route_id for r in plan.routes]})
            return {
                "wave_id": wave_id,
                "routes": [
                    {
                        "route_id": route.route_id,
                        "vehicle_id": route.vehicle.vehicle_id,
                        "stops": [
                            {
                                "store_id": stop.store_id,
                                "eta": _fmt(stop.eta),
                                "containers": plan.assignments.get(
                                    (route.route_id, stop.store_id), []),
                            }
                            for stop in route.stops
                        ],
                    }
                    for route in plan.routes
                ],
                "splits": plan.splits,
            }

    # ------------------------------------------------------------------
    # 差异单：截单后改变未封签部分的唯一途径
    # ------------------------------------------------------------------
    def submit_difference(self, route_id: str, changes: list, *,
                          reason: str, created_at, diff_id: str | None = None) -> str:
        created = _fmt(_parse_ts(created_at))
        with self._lock, self.conn:
            self._must("route", "route_id", route_id)
            if not changes:
                raise StateError("差异单至少需要一行变更")
            for change in changes:
                if change["change_type"] not in ("add", "adjust", "remove"):
                    raise StateError(f"未知变更类型：{change['change_type']}")
                self._must("product", "product_id", change["product_id"])
                if change["change_type"] != "remove" and change["qty"] <= 0:
                    raise StateError("变更数量必须为正数")
            if diff_id is None:
                seq = self._row(
                    "select count(*) as n from difference_order where route_id = ?",
                    (route_id,))["n"] + 1
                diff_id = f"DIFF-{route_id}-{seq}"
            self.conn.execute(
                "insert into difference_order(diff_id, route_id, reason, status, created_at)"
                " values (?, ?, ?, 'pending', ?)",
                (diff_id, route_id, reason, created),
            )
            for line_no, change in enumerate(changes, start=1):
                self.conn.execute(
                    "insert into difference_line(diff_id, line_no, change_type, store_id,"
                    " product_id, qty) values (?, ?, ?, ?, ?, ?)",
                    (diff_id, line_no, change["change_type"], change["store_id"],
                     change["product_id"], float(change.get("qty", 0))),
                )
            self._emit("difference.submitted", diff_id, created,
                       {"route_id": route_id, "reason": reason})
            return diff_id

    def approve_difference(self, diff_id: str, *, decided_by: str, at) -> int:
        """批准并应用差异单，生成新的配送版本；已封签线路一律拒绝。

        返回新的配送版本号。
        """
        at = _fmt(_parse_ts(at))
        with self._lock, self.conn:
            diff = self._must("difference_order", "diff_id", diff_id)
            if diff["status"] != "pending":
                raise StateError(f"差异单 {diff_id} 已处理（{diff['status']}）")
            route = self._must("route", "route_id", diff["route_id"])
            route_id = route["route_id"]
            if route["status"] not in ("planned", "loading"):
                seal = self._row("select sealed_at from active_seal where route_id = ?",
                                 (route_id,))
                hint = f"，封签时间 {seal['sealed_at']}" if seal else ""
                raise StateError(
                    f"线路 {route_id} 状态为 {route['status']}{hint}，"
                    "差异单只能改变尚未封签的部分"
                )
            if self._row("select 1 from active_seal where route_id = ?", (route_id,)):
                raise StateError(f"线路 {route_id} 已封签，差异单无法应用")
            changes = self._rows(
                "select * from difference_line where diff_id = ? order by line_no",
                (diff_id,))
            affected_stores = {c["store_id"] for c in changes}
            loaded_stores = {
                row["store_id"]
                for row in self._rows(
                    "select a.store_id from container_assignment a"
                    " join container c on c.container_id = a.container_id"
                    " where a.route_id = ? and a.version_no = ? and c.status != 'at_warehouse'",
                    (route_id, route["current_version"]))
            }
            clash = affected_stores & loaded_stores
            if clash:
                raise StateError(
                    f"门店 {'、'.join(sorted(clash))} 的周转箱已装车，请先卸回再变更"
                )
            current = route["current_version"]
            new_version = current + 1
            lines = {
                (row["store_id"], row["product_id"]): dict(row)
                for row in self._rows(
                    "select * from version_line where route_id = ? and version_no = ?",
                    (route_id, current))
            }
            stops_on_route = {
                row["store_id"]
                for row in self._rows("select store_id from route_stop where route_id = ?",
                                      (route_id,))
            }
            for change in changes:
                key = (change["store_id"], change["product_id"])
                if change["change_type"] == "add":
                    if change["store_id"] not in stops_on_route:
                        raise StateError(
                            f"门店 {change['store_id']} 不在线路 {route_id} 上，不能加单"
                        )
                    if key in lines:
                        lines[key]["qty"] += change["qty"]
                    else:
                        order = self._row(
                            "select o.order_id from store_order o"
                            " where o.store_id = ? and o.wave_id = ?"
                            " and o.status != 'merged'",
                            (change["store_id"], route["wave_id"]))
                        lines[key] = {
                            "store_id": change["store_id"],
                            "product_id": change["product_id"],
                            "order_id": order["order_id"] if order else "",
                            "qty": float(change["qty"]),
                        }
                elif change["change_type"] == "adjust":
                    if key not in lines:
                        raise StateError(
                            f"线路 {route_id} 清单中没有 "
                            f"{change['store_id']}/{change['product_id']}，无法调整"
                        )
                    lines[key]["qty"] = float(change["qty"])
                else:  # remove
                    if key not in lines:
                        raise StateError(
                            f"线路 {route_id} 清单中没有 "
                            f"{change['store_id']}/{change['product_id']}，无法移除"
                        )
                    del lines[key]
            # 复制现有分配，再按变更后的重量增减受影响门店的周转箱。
            assignments = {
                row["container_id"]: row["store_id"]
                for row in self._rows(
                    "select container_id, store_id from container_assignment"
                    " where route_id = ? and version_no = ?",
                    (route_id, current))
            }
            capacity = self._must("wave", "wave_id", route["wave_id"])["container_capacity"]
            weights: dict[str, float] = {}
            for line in lines.values():
                product = self._must("product", "product_id", line["product_id"])
                weights[line["store_id"]] = (weights.get(line["store_id"], 0.0)
                                           + line["qty"] * product["unit_weight"])
            for store_id in affected_stores:
                assigned = sorted(cid for cid, sid in assignments.items() if sid == store_id)
                needed = ceil(weights.get(store_id, 0.0) / capacity) \
                    if weights.get(store_id, 0.0) > 0 else 0
                if needed > len(assigned):
                    pool = [
                        row["container_id"]
                        for row in self._rows(
                            "select container_id from container"
                            " where status = 'at_warehouse' order by container_id")
                        if row["container_id"] not in assignments
                    ]
                    if needed - len(assigned) > len(pool):
                        raise StateError("周转箱不足，无法为变更后的清单分配容器")
                    for container_id in pool[:needed - len(assigned)]:
                        assignments[container_id] = store_id
                elif needed < len(assigned):
                    for container_id in assigned[needed:]:
                        del assignments[container_id]
            # 清理失去全部商品的停靠点及其分配（纯回收任务的停靠点保留）。
            surviving_stores = {line["store_id"] for line in lines.values()}
            for stop in self._rows(
                    "select seq, store_id from route_stop where route_id = ?", (route_id,)):
                if stop["store_id"] not in surviving_stores and not any(
                        sid == stop["store_id"] for sid in assignments.values()):
                    # 纯回收任务的停靠点没有商品行，保留；有商品但被删光的停靠点移除。
                    has_recovery = self._row(
                        "select 1 from recovery_task where store_id = ? and wave_id = ?",
                        (stop["store_id"], route["wave_id"]))
                    if has_recovery is None:
                        self.conn.execute(
                            "delete from route_stop where route_id = ? and seq = ?",
                            (route_id, stop["seq"]))
                        assignments = {cid: sid for cid, sid in assignments.items()
                                       if sid != stop["store_id"]}
            self.conn.execute(
                "update delivery_version set status = 'superseded'"
                " where route_id = ? and version_no = ?",
                (route_id, current),
            )
            self.conn.execute(
                "insert into delivery_version(route_id, version_no, cause, status,"
                " created_at) values (?, ?, ?, 'published', ?)",
                (route_id, new_version, f"difference:{diff_id}", at),
            )
            for line in lines.values():
                self.conn.execute(
                    "insert into version_line(route_id, version_no, store_id, order_id,"
                    " product_id, qty) values (?, ?, ?, ?, ?, ?)",
                    (route_id, new_version, line["store_id"], line["order_id"],
                     line["product_id"], line["qty"]),
                )
            for container_id, store_id in assignments.items():
                self.conn.execute(
                    "insert into container_assignment(route_id, version_no, container_id,"
                    " store_id) values (?, ?, ?, ?)",
                    (route_id, new_version, container_id, store_id),
                )
            self.conn.execute(
                "update route set current_version = ? where route_id = ?",
                (new_version, route_id),
            )
            self.conn.execute(
                "update difference_order set status = 'applied', decided_by = ?,"
                " decided_at = ?, applied_version = ? where diff_id = ?",
                (decided_by, at, new_version, diff_id),
            )
            self._emit("difference.approved", diff_id, at,
                       {"route_id": route_id, "version_no": new_version})
            return new_version

    def reject_difference(self, diff_id: str, *, decided_by: str, at) -> None:
        at = _fmt(_parse_ts(at))
        with self._lock, self.conn:
            diff = self._must("difference_order", "diff_id", diff_id)
            if diff["status"] != "pending":
                raise StateError(f"差异单 {diff_id} 已处理（{diff['status']}）")
            self.conn.execute(
                "update difference_order set status = 'rejected', decided_by = ?,"
                " decided_at = ? where diff_id = ?",
                (decided_by, at, diff_id),
            )
            self._emit("difference.rejected", diff_id, at)

    # ------------------------------------------------------------------
    # 配送清单（司机领单）
    # ------------------------------------------------------------------
    def get_manifest(self, route_id: str, version_no: int | None = None) -> dict:
        with self._lock:
            route = self._must("route", "route_id", route_id)
            version_no = version_no or route["current_version"]
            version = self._row(
                "select * from delivery_version where route_id = ? and version_no = ?",
                (route_id, version_no))
            if version is None:
                raise NotFoundError(f"线路 {route_id} 不存在版本 v{version_no}")
            seal = self._row("select seal_no from active_seal where route_id = ?",
                             (route_id,))
            stops = []
            for stop in self._rows(
                    "select * from route_stop where route_id = ? order by seq",
                    (route_id,)):
                lines = self._rows(
                    "select product_id, qty from version_line"
                    " where route_id = ? and version_no = ? and store_id = ?"
                    " order by product_id",
                    (route_id, version_no, stop["store_id"]))
                containers = [
                    row["container_id"]
                    for row in self._rows(
                        "select container_id from container_assignment"
                        " where route_id = ? and version_no = ? and store_id = ?"
                        " order by container_id",
                        (route_id, version_no, stop["store_id"]))
                ]
                stops.append({
                    "store_id": stop["store_id"],
                    "eta": stop["eta"],
                    "lines": [dict(line) for line in lines],
                    "containers": containers,
                })
            return {
                "route_id": route_id,
                "wave_id": route["wave_id"],
                "vehicle_id": route["vehicle_id"],
                "version_no": version_no,
                "route_status": route["status"],
                "seal_no": seal["seal_no"] if seal else None,
                "stops": stops,
            }

    def pull_manifest(self, route_id: str, *, pulled_by: str, at) -> dict:
        """司机领单：记录领取人与领取时的配送版本。"""
        at = _fmt(_parse_ts(at))
        with self._lock, self.conn:
            route = self._must("route", "route_id", route_id)
            self.conn.execute(
                "insert into manifest_pull(route_id, version_no, pulled_by, pulled_at)"
                " values (?, ?, ?, ?)",
                (route_id, route["current_version"], pulled_by, at),
            )
            self._emit("manifest.pulled", route_id, at,
                       {"pulled_by": pulled_by,
                        "version_no": route["current_version"]})
            return self.get_manifest(route_id)

    # ------------------------------------------------------------------
    # 扫描交接（离线补传、幂等）
    # ------------------------------------------------------------------
    def _version_at(self, route_id: str, occurred: datetime) -> int | None:
        """业务发生时间点上线路的有效配送版本。"""
        valid = None
        for row in self._rows(
                "select version_no, created_at from delivery_version"
                " where route_id = ? order by version_no",
                (route_id,)):
            if _parse_ts(row["created_at"]) <= occurred:
                valid = row["version_no"]
        return valid

    def _accepted_scans(self, container_id: str):
        return self._rows(
            "select * from scan_event where container_id = ? and result = 'accepted'"
            " order by occurred_at, received_at, scan_id",
            (container_id,))

    def record_scan(self, scan_id: str, container_id: str, action: str,
                    route_id: str, *, version_no: int, occurred_at,
                    store_id: str | None = None, received_at=None) -> dict:
        """记录一次扫描。重复上传与违规扫描都不会改变业务状态，只留痕。

        返回 {"result": "accepted" | "duplicate" | "rejected", "reason": ...}。
        """
        occurred = _parse_ts(occurred_at)
        received = _fmt(_parse_ts(received_at)) if received_at is not None \
            else _fmt(occurred)
        occurred_s = _fmt(occurred)
        with self._lock, self.conn:
            prior = self._row("select result from scan_event where scan_id = ?",
                              (scan_id,))
            if prior is not None and prior["result"] in ("accepted", "duplicate"):
                return {"scan_id": scan_id, "result": "duplicate",
                        "reason": "扫描已受理，重复上传不再生效"}

            def finish(result: str, reason: str | None) -> dict:
                if prior is None:
                    self.conn.execute(
                        "insert into scan_event(scan_id, container_id, action, route_id,"
                        " store_id, version_no, occurred_at, received_at, result, reason)"
                        " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (scan_id, container_id, action, route_id, store_id, version_no,
                         occurred_s, received, result, reason),
                    )
                else:
                    # 曾被拒绝的扫描允许重传，以本次上报的业务信息为准。
                    self.conn.execute(
                        "update scan_event set container_id = ?, action = ?, route_id = ?,"
                        " store_id = ?, version_no = ?, occurred_at = ?, received_at = ?,"
                        " result = ?, reason = ? where scan_id = ?",
                        (container_id, action, route_id, store_id, version_no,
                         occurred_s, received, result, reason, scan_id),
                    )
                return {"scan_id": scan_id, "result": result, "reason": reason}

            if action not in SCAN_TRANSITIONS:
                return finish("rejected", f"未知扫描动作：{action}")
            container = self._row("select * from container where container_id = ?",
                                  (container_id,))
            if container is None:
                return finish("rejected", f"周转箱不存在：{container_id}")
            route = self._row("select * from route where route_id = ?", (route_id,))
            if route is None:
                return finish("rejected", f"线路不存在：{route_id}")
            valid_version = self._version_at(route_id, occurred)
            if valid_version is None:
                return finish("rejected", "业务发生时间早于线路首个配送版本")
            if version_no != valid_version:
                return finish(
                    "rejected",
                    f"配送版本不一致：业务发生时有效版本为 v{valid_version}，"
                    f"扫描上报 v{version_no}")
            # 同一趟行程内的业务键去重（上次返仓之后的扫描属于本趟）。
            history = self._accepted_scans(container_id)
            last_return = max(
                (_parse_ts(s["occurred_at"]) for s in history if s["action"] == "return"),
                default=None,
            )
            trip_scans = [s for s in history
                          if last_return is None
                          or _parse_ts(s["occurred_at"]) > last_return]
            if any(s["action"] == action and s["route_id"] == route_id
                   for s in trip_scans):
                return finish("duplicate", "重复扫描：同一业务已受理，不再装车或签收")
            # 动作级业务校验。
            if action == "load":
                if route["status"] not in ("planned", "loading"):
                    return finish("rejected",
                                  f"线路状态为 {route['status']}，不能装车")
                owner = self._row(
                    "select route_id from container_assignment"
                    " where container_id = ? and route_id != ?"
                    " order by version_no desc limit 1",
                    (container_id, route_id))
                allocated = self._row(
                    "select 1 from container_assignment"
                    " where route_id = ? and version_no = ? and container_id = ?",
                    (route_id, valid_version, container_id))
                if allocated is None:
                    if owner is not None:
                        return finish(
                            "rejected",
                            f"周转箱属于线路 {owner['route_id']}，不能装上本线路")
                    return finish("rejected", "周转箱不在本线路装箱清单中")
            elif action == "handover":
                if store_id is None:
                    return finish("rejected", "签收扫描缺少门店")
                if route["status"] != "departed":
                    return finish("rejected",
                                  f"线路状态为 {route['status']}，尚未发车，不能签收")
                if occurred < _parse_ts(route["departed_at"]):
                    return finish(
                        "rejected",
                        f"签收时间 {occurred_s} 早于发车时间 {route['departed_at']}")
                assignment = self._row(
                    "select store_id from container_assignment"
                    " where route_id = ? and version_no = ? and container_id = ?",
                    (route_id, valid_version, container_id))
                if assignment is None or assignment["store_id"] != store_id:
                    return finish("rejected", "周转箱不应由该门店签收")
            elif action == "recover":
                if store_id is None:
                    return finish("rejected", "回收扫描缺少门店")
                if container["status"] != "handed_over" \
                        or container["current_store_id"] != store_id:
                    return finish("rejected", "周转箱不在该门店，无法回收")
            else:  # return
                if container["current_route_id"] != route_id:
                    return finish("rejected", "周转箱不在该线路上，无法确认返仓")
            # 时序合法性：按业务发生时间插入容器履历后，相邻动作必须合法。
            timeline = sorted(
                [(s["action"], _parse_ts(s["occurred_at"])) for s in history]
                + [(action, occurred)],
                key=lambda item: item[1],
            )
            actions = [item[0] for item in timeline]
            legal = actions[0] == FIRST_ACTION and all(
                actions[i + 1] in SCAN_TRANSITIONS[actions[i]]
                for i in range(len(actions) - 1)
            )
            if not legal:
                return finish(
                    "rejected",
                    f"扫描时序不合法：容器当前为 {container['status']}，"
                    f"不能执行 {action}")
            # 生效：更新容器状态与位置（以业务发生时间最晚的动作为准）。
            finish("accepted", None)
            latest = max(
                [(s["action"], _parse_ts(s["occurred_at"]),
                  s["route_id"], s["store_id"]) for s in history]
                + [(action, occurred, route_id, store_id)],
                key=lambda item: item[1],
            )
            latest_action, _, latest_route, latest_store = latest
            new_status = CONTAINER_STATE_AFTER[latest_action]
            if latest_action == "load":
                loc_route, loc_store = latest_route, None
            elif latest_action == "handover":
                loc_route, loc_store = None, latest_store
            elif latest_action == "recover":
                loc_route, loc_store = latest_route, None
            else:
                loc_route, loc_store = None, None
            self.conn.execute(
                "update container set status = ?, current_route_id = ?,"
                " current_store_id = ? where container_id = ?",
                (new_status, loc_route, loc_store, container_id),
            )
            if action == "load" and route["status"] == "planned":
                self.conn.execute(
                    "update route set status = 'loading' where route_id = ?",
                    (route_id,))
            self._emit(SCAN_EVENT_TYPE[action], container_id, occurred_s,
                       {"route_id": route_id, "store_id": store_id,
                        "version_no": version_no})
            if action == "handover":
                remaining = self._row(
                    "select count(*) as n from container_assignment a"
                    " join container c on c.container_id = a.container_id"
                    " where a.route_id = ? and a.version_no = ? and a.store_id = ?"
                    " and c.status = 'loaded'",
                    (route_id, route["current_version"], store_id))["n"]
                if remaining == 0 and self._row(
                        "select 1 from event_log where event_type = 'store.signed'"
                        " and aggregate_id = ?",
                        (f"{route_id}:{store_id}",)) is None:
                    self._emit("store.signed", f"{route_id}:{store_id}", occurred_s,
                               {"route_id": route_id, "store_id": store_id})
            return {"scan_id": scan_id, "result": "accepted", "reason": None}

    # ------------------------------------------------------------------
    # 封签与发车
    # ------------------------------------------------------------------
    def seal_route(self, route_id: str, *, version_no: int, seal_no: str,
                   operator: str, at) -> dict:
        """车辆封签。并发封签只保留一个结果，其余得到 SealConflictError。"""
        at = _fmt(_parse_ts(at))
        with self._lock, self.conn:
            route = self._must("route", "route_id", route_id)
            if self._row("select 1 from active_seal where route_id = ?",
                         (route_id,)) is not None:
                raise SealConflictError(f"线路 {route_id} 已存在有效封签")
            if route["status"] == "planned":
                raise StateError(f"线路 {route_id} 尚未开始装车，不能封签")
            if route["status"] != "loading":
                raise StateError(
                    f"线路 {route_id} 状态为 {route['status']}，不能封签")
            if version_no != route["current_version"]:
                raise VersionMismatchError(
                    f"封签版本 v{version_no} 与当前配送版本 "
                    f"v{route['current_version']} 不一致")
            unloaded = [
                row["container_id"]
                for row in self._rows(
                    "select a.container_id from container_assignment a"
                    " join container c on c.container_id = a.container_id"
                    " where a.route_id = ? and a.version_no = ?"
                    " and c.status != 'loaded' order by a.container_id",
                    (route_id, version_no))
            ]
            if unloaded:
                raise StateError(
                    f"清单未装箱完成，不能封签：{'、'.join(unloaded)}")
            try:
                self.conn.execute(
                    "insert into active_seal(route_id, version_no, seal_no, sealed_at,"
                    " operator) values (?, ?, ?, ?, ?)",
                    (route_id, version_no, seal_no, at, operator),
                )
            except sqlite3.IntegrityError as exc:
                raise SealConflictError(
                    f"线路 {route_id} 封签冲突，只保留先到的封签结果") from exc
            self.conn.execute(
                "update route set status = 'sealed' where route_id = ?", (route_id,))
            self._emit("vehicle.sealed", route_id, at,
                       {"seal_no": seal_no, "version_no": version_no,
                        "vehicle_id": route["vehicle_id"]})
            return {"route_id": route_id, "seal_no": seal_no,
                    "version_no": version_no, "sealed_at": at}

    def depart_route(self, route_id: str, *, at) -> None:
        at = _fmt(_parse_ts(at))
        with self._lock, self.conn:
            route = self._must("route", "route_id", route_id)
            if route["status"] != "sealed":
                raise StateError(f"线路 {route_id} 未封签，不能发车")
            self.conn.execute(
                "update route set status = 'departed', departed_at = ? where route_id = ?",
                (at, route_id))
            self.conn.execute(
                "update vehicle set status = 'en_route' where vehicle_id = ?",
                (route["vehicle_id"],))
            self.conn.execute(
                "update wave set status = 'in_progress'"
                " where wave_id = ? and status = 'planned'",
                (route["wave_id"],))
            self._emit("route.departed", route_id, at)

    def complete_route(self, route_id: str, *, at) -> None:
        at = _fmt(_parse_ts(at))
        with self._lock, self.conn:
            route = self._must("route", "route_id", route_id)
            if route["status"] != "departed":
                raise StateError(f"线路 {route_id} 状态为 {route['status']}，不能完结")
            on_board = self._row(
                "select count(*) as n from container_assignment a"
                " join container c on c.container_id = a.container_id"
                " where a.route_id = ? and a.version_no = ? and c.status = 'loaded'",
                (route_id, route["current_version"]))["n"]
            if on_board:
                raise StateError(f"线路 {route_id} 仍有 {on_board} 只周转箱未签收")
            self.conn.execute(
                "update route set status = 'completed' where route_id = ?", (route_id,))
            self.conn.execute(
                "update vehicle set status = 'available' where vehicle_id = ?",
                (route["vehicle_id"],))
            orders = self._rows(
                "select distinct order_id from version_line"
                " where route_id = ? and version_no = ?",
                (route_id, route["current_version"]))
            for order in orders:
                shortage = self._row(
                    "select 1 from order_exception where order_id = ?"
                    " and type = 'out_of_stock' limit 1",
                    (order["order_id"],))
                self.conn.execute(
                    "update store_order set status = ? where order_id = ?",
                    ("partial" if shortage else "fulfilled", order["order_id"]))
            remaining = self._row(
                "select count(*) as n from route where wave_id = ?"
                " and status != 'completed'",
                (route["wave_id"],))["n"]
            if remaining == 0:
                self.conn.execute(
                    "update wave set status = 'completed' where wave_id = ?",
                    (route["wave_id"],))
            self._emit("route.completed", route_id, at)

    # ------------------------------------------------------------------
    # 车辆故障与整车转派
    # ------------------------------------------------------------------
    def report_breakdown(self, vehicle_id: str, *, at,
                         reason: str = "车辆故障") -> list:
        """上报车辆故障：在途线路进入待转派，有效封签作废留痕。"""
        at = _fmt(_parse_ts(at))
        with self._lock, self.conn:
            vehicle = self._must("vehicle", "vehicle_id", vehicle_id)
            if vehicle["status"] == "broken_down":
                raise StateError(f"车辆 {vehicle_id} 已处于故障状态")
            self.conn.execute(
                "update vehicle set status = 'broken_down' where vehicle_id = ?",
                (vehicle_id,))
            affected = []
            for route in self._rows(
                    "select * from route where vehicle_id = ?"
                    " and status in ('loading', 'sealed', 'departed')",
                    (vehicle_id,)):
                self.conn.execute(
                    "update route set status = 'pending_transfer', departed_at = NULL"
                    " where route_id = ?",
                    (route["route_id"],))
                seal = self._row("select * from active_seal where route_id = ?",
                                 (route["route_id"],))
                if seal is not None:
                    self.conn.execute(
                        "delete from active_seal where route_id = ?",
                        (route["route_id"],))
                    self.conn.execute(
                        "insert into seal_history(route_id, version_no, seal_no,"
                        " sealed_at, operator, voided_at, void_reason)"
                        " values (?, ?, ?, ?, ?, ?, ?)",
                        (route["route_id"], seal["version_no"], seal["seal_no"],
                         seal["sealed_at"], seal["operator"], at, reason),
                    )
                    self._emit("vehicle.seal_voided", route["route_id"], at,
                               {"seal_no": seal["seal_no"], "reason": reason})
                affected.append(route["route_id"])
            self._emit("vehicle.breakdown", vehicle_id, at,
                       {"reason": reason, "routes": affected})
            return affected

    def transfer_route(self, route_id: str, to_vehicle_id: str, *, at,
                       reason: str = "车辆故障整车转派") -> str:
        """整车转派：车上容器随线路整体换车，已交接容器不受影响。"""
        at = _fmt(_parse_ts(at))
        with self._lock, self.conn:
            route = self._must("route", "route_id", route_id)
            if route["status"] != "pending_transfer":
                raise StateError(
                    f"线路 {route_id} 状态为 {route['status']}，不在待转派环节")
            target = self._must("vehicle", "vehicle_id", to_vehicle_id)
            if target["status"] != "available":
                raise TransferError(
                    f"目标车辆 {to_vehicle_id} 状态为 {target['status']}，不可用")
            signed_stores = {
                row["store_id"]
                for row in self._rows(
                    "select a.store_id from container_assignment a"
                    " join container c on c.container_id = a.container_id"
                    " where a.route_id = ? and a.version_no = ?"
                    " group by a.store_id"
                    " having sum(c.status in ('at_warehouse', 'loaded')) = 0",
                    (route_id, route["current_version"]))
            }
            remaining_weight = 0.0
            zones = set()
            for line in self._rows(
                    "select v.store_id, v.qty, p.unit_weight, p.temp_zone"
                    " from version_line v"
                    " join product p on p.product_id = v.product_id"
                    " where v.route_id = ? and v.version_no = ?",
                    (route_id, route["current_version"])):
                if line["store_id"] in signed_stores:
                    continue
                remaining_weight += line["qty"] * line["unit_weight"]
                zones.add(line["temp_zone"])
            target_zones = set(json.loads(target["temp_zones"]))
            if not zones <= target_zones:
                raise TransferError(
                    f"目标车辆 {to_vehicle_id} 温层不满足：需要 {sorted(zones)}")
            if remaining_weight > target["capacity"] + 1e-9:
                raise TransferError(
                    f"目标车辆 {to_vehicle_id} 载重不足："
                    f"剩余 {remaining_weight}，额定 {target['capacity']}")
            transfer_id = f"TR-{route_id}-{uuid.uuid4().hex[:8]}"
            self.conn.execute(
                "insert into transfer(transfer_id, route_id, from_vehicle, to_vehicle,"
                " reason, created_at) values (?, ?, ?, ?, ?, ?)",
                (transfer_id, route_id, route["vehicle_id"], to_vehicle_id, reason, at),
            )
            self.conn.execute(
                "update route set vehicle_id = ?, status = 'loading'"
                " where route_id = ?",
                (to_vehicle_id, route_id),
            )
            self._emit("vehicle.transferred", route_id, at,
                       {"from": route["vehicle_id"], "to": to_vehicle_id,
                        "reason": reason})
            return transfer_id

    # ------------------------------------------------------------------
    # 异常登记：缺货与延迟
    # ------------------------------------------------------------------
    def record_shortage(self, route_id: str, store_id: str, product_id: str, *,
                        qty: float, reason: str, at) -> None:
        """仓库装箱时登记缺货，门店查询可见具体原因。"""
        at = _fmt(_parse_ts(at))
        with self._lock, self.conn:
            route = self._must("route", "route_id", route_id)
            line = self._row(
                "select qty from version_line where route_id = ? and version_no = ?"
                " and store_id = ? and product_id = ?",
                (route_id, route["current_version"], store_id, product_id))
            if line is None:
                raise NotFoundError(
                    f"线路 {route_id} 清单中没有 {store_id}/{product_id}")
            if qty <= 0 or qty > line["qty"]:
                raise StateError("缺货数量必须在清单数量以内且为正数")
            order = self._row(
                "select order_id from store_order where store_id = ? and wave_id = ?"
                " and status != 'merged'",
                (store_id, route["wave_id"]))
            self.conn.execute(
                "insert into order_exception(order_id, route_id, store_id, type,"
                " product_id, qty, detail, created_at)"
                " values (?, ?, ?, 'out_of_stock', ?, ?, ?, ?)",
                (order["order_id"] if order else None, route_id, store_id,
                 product_id, float(qty), reason, at),
            )
            self._emit("order.shortage", route_id, at,
                       {"store_id": store_id, "product_id": product_id,
                        "qty": qty, "reason": reason})

    def report_delay(self, route_id: str, *, minutes: int, reason: str, at) -> None:
        """登记线路延迟，沿途门店查询可见原因与新的预计到达时间。"""
        at = _fmt(_parse_ts(at))
        with self._lock, self.conn:
            route = self._must("route", "route_id", route_id)
            if route["status"] not in ("planned", "loading", "sealed", "departed"):
                raise StateError(f"线路 {route_id} 状态为 {route['status']}，不能登记延迟")
            self.conn.execute(
                "update route set delay_minutes = delay_minutes + ?,"
                " delay_reason = ? where route_id = ?",
                (int(minutes), reason, route_id),
            )
            for order in self._rows(
                    "select distinct v.order_id, v.store_id from version_line v"
                    " where v.route_id = ? and v.version_no = ?",
                    (route_id, route["current_version"])):
                self.conn.execute(
                    "insert into order_exception(order_id, route_id, store_id, type,"
                    " detail, created_at) values (?, ?, ?, 'delayed', ?, ?)",
                    (order["order_id"], route_id, order["store_id"],
                     f"延迟 {minutes} 分钟：{reason}", at),
                )
            self._emit("route.delayed", route_id, at,
                       {"minutes": minutes, "reason": reason})

    # ------------------------------------------------------------------
    # 查询：门店视图、线路回看、恢复报告
    # ------------------------------------------------------------------
    def store_view(self, store_id: str, wave_id: str) -> dict:
        """门店视角：商品行的计划/缺货/拆单情况、停靠进度与异常原因。"""
        with self._lock:
            self._must("store", "store_id", store_id)
            self._must("wave", "wave_id", wave_id)
            orders = self._rows(
                "select * from store_order where store_id = ? and wave_id = ?"
                " and status != 'merged'",
                (store_id, wave_id))
            order_ids = [o["order_id"] for o in orders]
            ordered = {}
            if order_ids:
                marks = ",".join("?" * len(order_ids))
                for row in self._rows(
                        "select product_id, sum(qty) as qty from order_line"
                        f" where order_id in ({marks}) group by product_id",
                        tuple(order_ids)):
                    ordered[row["product_id"]] = row["qty"]
            placements = {}
            stops = []
            for stop in self._rows(
                    "select s.*, r.vehicle_id, r.status as route_status,"
                    " r.current_version, r.delay_minutes, r.delay_reason"
                    " from route_stop s join route r on r.route_id = s.route_id"
                    " where s.store_id = ? and r.wave_id = ? order by s.eta",
                    (store_id, wave_id)):
                for line in self._rows(
                        "select product_id, qty from version_line"
                        " where route_id = ? and version_no = ? and store_id = ?",
                        (stop["route_id"], stop["current_version"], store_id)):
                    placements.setdefault(line["product_id"], []).append({
                        "route_id": stop["route_id"],
                        "qty": line["qty"],
                        "route_status": stop["route_status"],
                    })
                assigned = self._rows(
                    "select c.status from container_assignment a"
                    " join container c on c.container_id = a.container_id"
                    " where a.route_id = ? and a.version_no = ? and a.store_id = ?",
                    (stop["route_id"], stop["current_version"], store_id))
                handed = sum(1 for c in assigned if c["status"] != "loaded")
                seal = self._row("select seal_no from active_seal where route_id = ?",
                                 (stop["route_id"],))
                eta = _parse_ts(stop["eta"])
                stops.append({
                    "route_id": stop["route_id"],
                    "vehicle_id": stop["vehicle_id"],
                    "route_status": stop["route_status"],
                    "eta": _fmt(eta),
                    "eta_delayed": _fmt(eta + timedelta(minutes=stop["delay_minutes"])),
                    "delay_reason": stop["delay_reason"],
                    "seal_no": seal["seal_no"] if seal else None,
                    "containers_assigned": len(assigned),
                    "containers_handed_over": handed,
                    "signed": len(assigned) > 0 and handed == len(assigned),
                })
            shortages = {}
            for row in self._rows(
                    "select product_id, sum(qty) as qty from order_exception"
                    " where store_id = ? and type = 'out_of_stock'"
                    " group by product_id",
                    (store_id,)):
                shortages[row["product_id"]] = row["qty"]
            lines = []
            for product_id in sorted(set(ordered) | set(placements) | set(shortages)):
                lines.append({
                    "product_id": product_id,
                    "qty_ordered": ordered.get(product_id, 0),
                    "qty_planned": sum(p["qty"] for p in placements.get(product_id, [])),
                    "qty_short": shortages.get(product_id, 0),
                    "placements": placements.get(product_id, []),
                })
            exceptions = []
            if order_ids:
                marks = ",".join("?" * len(order_ids))
                for row in self._rows(
                        "select * from order_exception"
                        f" where order_id in ({marks}) order by exc_id",
                        tuple(order_ids)):
                    exceptions.append({
                        "type": row["type"],
                        "product_id": row["product_id"],
                        "qty": row["qty"],
                        "route_id": row["route_id"],
                        "detail": row["detail"],
                        "created_at": row["created_at"],
                    })
            return {
                "store_id": store_id,
                "wave_id": wave_id,
                "orders": [{"order_id": o["order_id"], "status": o["status"]}
                           for o in orders],
                "lines": lines,
                "stops": stops,
                "exceptions": exceptions,
            }

    def route_history(self, route_id: str) -> dict:
        """线路回看：版本、封签、转派、扫描与差异单，改单时点一目了然。"""
        with self._lock:
            route = self._must("route", "route_id", route_id)
            versions = [dict(row) for row in self._rows(
                "select * from delivery_version where route_id = ?"
                " order by version_no", (route_id,))]
            seals = [dict(row) | {"voided_at": None, "void_reason": None}
                     for row in self._rows(
                         "select * from active_seal where route_id = ?", (route_id,))]
            seals += [dict(row) for row in self._rows(
                "select * from seal_history where route_id = ?", (route_id,))]
            seals.sort(key=lambda s: s["sealed_at"])
            return {
                "route_id": route_id,
                "vehicle_id": route["vehicle_id"],
                "status": route["status"],
                "current_version": route["current_version"],
                "versions": versions,
                "seals": seals,
                "transfers": [dict(row) for row in self._rows(
                    "select * from transfer where route_id = ? order by created_at",
                    (route_id,))],
                "scans": [dict(row) for row in self._rows(
                    "select * from scan_event where route_id = ?"
                    " order by occurred_at, received_at", (route_id,))],
                "differences": [dict(row) for row in self._rows(
                    "select * from difference_order where route_id = ?"
                    " order by created_at", (route_id,))],
            }

    def recovery_report(self) -> dict:
        """恢复运行报告：待转派车辆与未回收容器当前所处的环节。"""
        with self._lock:
            pending = []
            for route in self._rows(
                    "select * from route where status = 'pending_transfer'"
                    " order by route_id"):
                counts = self._row(
                    "select sum(c.status = 'loaded') as loaded,"
                    " sum(c.status = 'handed_over') as handed"
                    " from container_assignment a"
                    " join container c on c.container_id = a.container_id"
                    " where a.route_id = ? and a.version_no = ?",
                    (route["route_id"], route["current_version"]))
                pending.append({
                    "route_id": route["route_id"],
                    "vehicle_id": route["vehicle_id"],
                    "vehicle_status": self._must(
                        "vehicle", "vehicle_id", route["vehicle_id"])["status"],
                    "loaded_containers": counts["loaded"] or 0,
                    "handed_over_containers": counts["handed"] or 0,
                })
            unrecovered = [
                {
                    "container_id": row["container_id"],
                    "status": row["status"],
                    "route_id": row["current_route_id"],
                    "store_id": row["current_store_id"],
                }
                for row in self._rows(
                    "select * from container where status != 'at_warehouse'"
                    " order by container_id")
            ]
            return {
                "pending_transfers": pending,
                "unrecovered_containers": unrecovered,
                "broken_vehicles": [
                    row["vehicle_id"]
                    for row in self._rows(
                        "select vehicle_id from vehicle where status = 'broken_down'"
                        " order by vehicle_id")
                ],
            }
