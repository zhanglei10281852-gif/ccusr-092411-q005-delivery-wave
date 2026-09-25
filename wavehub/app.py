"""应用服务：波次编排与交接的全部业务用例。

一个 WaveHub = 一个事件存储 + 一个内存投影。所有命令显式传入业务发生时间
occurred_at（带时区）；recorded_at 由系统补登。离线补传与在线操作走同一套
业务时间校验，因此“改单在封签前还是封签后”由事件时间确定，可回看、可重放。
"""
from __future__ import annotations

import json
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from . import errors as E
from .clock import iso, parse_dt, utc_now
from .planner import CONTAINER_TARE_KG, Plan, build_plan
from .projection import Projection
from .store import EventStore


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class WaveHub:
    def __init__(self, store: EventStore | str | Path | None = None) -> None:
        self.store = store if isinstance(store, EventStore) else EventStore(store or ":memory:")
        self.projection = Projection()
        self.projection.load(self.store.replay())

    # --------------------------------------------------------------- 底层

    def _rebuild(self) -> None:
        self.projection = Projection()
        self.projection.load(self.store.replay())

    def _emit(self, event_type: str, aggregate_id: str, occurred_at: str | datetime,
              payload: dict[str, Any], delivery_version: str | None = None,
              event_id: str | None = None, *, commit: bool = True) -> dict[str, Any]:
        try:
            row = self.store.append(
                event_id=event_id or _new_id("evt"),
                event_type=event_type,
                aggregate_id=aggregate_id,
                occurred_at=iso(occurred_at),
                recorded_at=utc_now().isoformat(),
                payload=payload,
                delivery_version=delivery_version,
            )
        except Exception:
            self.store.rollback()
            self._rebuild()
            raise
        self.projection.apply(
            event_type, aggregate_id, payload,
            occurred_at=row["occurred_at"],
            delivery_version=delivery_version,
            event_id=row["event_id"],
        )
        if commit:
            self.store.commit()
        return {"event_id": row["event_id"], "seq": row["seq"]}

    # --------------------------------------------------------------- 建档

    def register_order(self, order: Any, occurred_at: str | datetime) -> dict[str, Any]:
        if order.order_id in self.projection.orders:
            raise ValueError(f"订单已存在：{order.order_id}")
        payload = {
            "store_id": order.store_id,
            "route_id": order.route_id,
            "window_open": iso(order.window_open),
            "window_close": iso(order.window_close),
            "service_minutes": order.service_minutes,
            "items": [
                {"sku": i.sku, "qty": i.qty, "zone": i.zone, "weight": i.weight,
                 "coload_tags": sorted(i.coload_tags), "conflicts": sorted(i.conflicts)}
                for i in order.items
            ],
        }
        return self._emit("order.received", order.order_id, occurred_at, payload)

    def register_vehicle(self, vehicle: Any, occurred_at: str | datetime) -> dict[str, Any]:
        if vehicle.vehicle_id in self.projection.vehicles:
            raise ValueError(f"车辆已存在：{vehicle.vehicle_id}")
        payload = {
            "plate": vehicle.plate,
            "capacity_kg": vehicle.capacity_kg,
            "zones": sorted(vehicle.zones),
            "route_minutes_limit": vehicle.route_minutes_limit,
            "base_minutes": vehicle.base_minutes,
        }
        return self._emit("vehicle.registered", vehicle.vehicle_id, occurred_at, payload)

    def register_container(self, container_id: str, route_id: str,
                           occurred_at: str | datetime, *, zone: str | None = None,
                           actor: str | None = None) -> dict[str, Any]:
        if container_id in self.projection.containers:
            raise ValueError(f"周转箱已存在：{container_id}")
        return self._emit("container.registered", container_id, occurred_at,
                          {"route_id": route_id, "zone": zone, "actor": actor})

    def receive_return_task(self, task: Any, occurred_at: str | datetime) -> dict[str, Any]:
        """箱筐回收需求入库（截单前与需求一同进入编排）。"""
        key = f"ret:{task.route_id}:{task.store_id}:{task.container_count}"
        payload = {"store_id": task.store_id, "route_id": task.route_id,
                   "container_count": task.container_count}
        return self._emit("return.task_received", key, occurred_at, payload)

    # --------------------------------------------------------------- 截单前合并

    def merge_orders(self, survivor_id: str, absorbed_id: str,
                     occurred_at: str | datetime) -> dict[str, Any]:
        """截单前合并同店同线路需求；任一订单所在波次已截单则拒绝。"""
        s = self.projection.orders.get(survivor_id)
        a = self.projection.orders.get(absorbed_id)
        if s is None or a is None:
            raise ValueError("待合并订单不存在")
        if not s["active"] or not a["active"]:
            raise E.BusinessRuleError(E.WAVE_CLOSED, "订单已失效，不能合并")
        if s["store_id"] != a["store_id"] or s["route_id"] != a["route_id"]:
            raise E.BusinessRuleError(E.WAVE_CLOSED, "仅同门店同线路需求可合并")
        t = parse_dt(occurred_at)
        wave_ids = None
        for oid in (survivor_id, absorbed_id):
            ids = self.projection.orders[oid]["wave_ids"]
            wave_ids = ids if wave_ids is None else wave_ids
            for wid in ids:
                w = self.projection.waves[wid]
                if w["state"] != "collecting":
                    raise E.BusinessRuleError(
                        E.WAVE_CLOSED,
                        f"订单 {oid} 所在波次 {wid} 已截单/封签，截单后不得直接合并",
                        wave_id=wid, cutoff_at=w.get("cutoff_at"),
                    )
        s_ids = set(self.projection.orders[survivor_id]["wave_ids"])
        a_ids = set(self.projection.orders[absorbed_id]["wave_ids"])
        if s_ids and a_ids and s_ids != a_ids:
            raise E.BusinessRuleError(
                E.WAVE_CLOSED, "两单已编入不同波次，不能跨波次合并",
                survivor_waves=sorted(s_ids), absorbed_waves=sorted(a_ids),
            )
        return self._emit("order.merged", survivor_id, t,
                          {"absorbed_order_id": absorbed_id})

    # --------------------------------------------------------------- 编排与截单

    def plan_waves(self, occurred_at: str | datetime) -> dict[str, Any]:
        p = self.projection
        route_orders: dict[str, list] = {}
        for o in p.orders.values():
            # 深拷贝：编排过程中拆单会改写订单行，投影只能由事件修改
            if o["active"] and not o["wave_ids"]:
                route_orders.setdefault(o["route_id"], []).append(deepcopy(o))
        if not route_orders:
            return {"waves": [], "splits": []}

        free_containers = {
            route_id: p.free_containers(route_id)
            for route_id in route_orders
        }
        return_tasks = {
            route_id: [dict(t) for t in p.return_tasks.get(route_id, [])
                       if t.get("planned_wave") is None]
            for route_id in route_orders
        }
        vehicles = [v for v in p.vehicles.values()
                    if v["status"] == "active" and not v.get("wave_id")]

        plan: Plan = build_plan(
            route_orders=route_orders,
            vehicles=vehicles,
            free_containers=free_containers,
            return_tasks=return_tasks,
            wave_seq=self._wave_seq_by_route(),
            earliest_depart=parse_dt(occurred_at),
        )

        results: list[dict[str, Any]] = []
        try:
            for split in plan.splits:
                self._emit_split(split, occurred_at, commit=False)
            for spec in plan.waves:
                results.append(self._emit_wave(spec, occurred_at, commit=False))
            self.store.commit()
        except Exception:
            self.store.rollback()
            self._rebuild()
            raise
        return {"waves": results, "splits": [s.__dict__ for s in plan.splits]}

    def _wave_seq_by_route(self) -> dict[str, int]:
        seq: dict[str, int] = {}
        for wid in self.projection.waves:
            prefix = "wave-"
            rest = wid[len(prefix):]
            route, _, num = rest.rpartition("-")
            if num.isdigit():
                seq[route] = max(seq.get(route, 0), int(num))
        return seq

    def _emit_split(self, split: Any, occurred_at, *, commit: bool = True) -> None:
        self._emit(
            "order.split", split.parent_order_id, occurred_at,
            {"child_order_id": split.child_order_id, "items": split.items,
             "reason": split.reason, "wave_id": split.wave_id},
            commit=commit,
        )

    def _emit_wave(self, spec: Any, occurred_at, *, commit: bool = True) -> dict[str, Any]:
        return_tasks = [
            {"task_id": t.get("task_id"), "store_id": t["store_id"],
             "route_id": t["route_id"], "container_count": t["container_count"]}
            for t in spec.return_tasks
        ]
        payload = {
            "route_id": spec.route_id,
            "vehicle_id": spec.vehicle_id,
            "depart_at": spec.depart_at,
            "assignments": spec.assignments,
            "return_tasks": return_tasks,
            "load_weight": spec.load_weight,
            "reasons": spec.reasons,
            "delivery_version": spec.delivery_version,
            "version_seq": 1,
        }
        result = self._emit("wave.planned", spec.wave_id, occurred_at, payload,
                            delivery_version=spec.delivery_version, commit=commit)
        result["wave_id"] = spec.wave_id
        result["route_id"] = spec.route_id
        result["vehicle_id"] = spec.vehicle_id
        result["depart_at"] = spec.depart_at
        result["load_weight"] = spec.load_weight
        result["return_tasks"] = return_tasks
        result["assignments"] = spec.assignments
        result["order_ids"] = spec.order_ids
        result["delivery_version"] = spec.delivery_version
        result["reasons"] = spec.reasons
        return result

    def cutoff_wave(self, wave_id: str, occurred_at: str | datetime) -> dict[str, Any]:
        wave = self._wave(wave_id)
        t = parse_dt(occurred_at)
        if wave["state"] != "collecting":
            raise E.BusinessRuleError(
                E.WAVE_CLOSED, f"波次 {wave_id} 当前状态 {wave['state']}，不可截单",
                wave_id=wave_id, state=wave["state"],
            )
        if wave["planned_at"] > t:
            raise E.BusinessRuleError(E.SCAN_OUT_OF_ORDER, "截单时间早于编排时间")
        return self._emit("wave.cutoff", wave_id, t, {"at": iso(t)})

    # --------------------------------------------------------------- 差异单

    def request_adjustment(self, wave_id: str, kind: str, occurred_at: str | datetime,
                           *, detail: dict[str, Any] | None = None,
                           reason: str | None = None) -> str:
        self._wave(wave_id)
        aid = _new_id("adj")
        self._emit("adjustment.requested", aid, occurred_at,
                   {"wave_id": wave_id, "kind": kind, "detail": detail or {},
                    "reason": reason})
        return aid

    def decide_adjustment(self, adjustment_id: str, approve: bool,
                          occurred_at: str | datetime, *, approver: str | None = None,
                          reason: str | None = None) -> dict[str, Any]:
        adj = self.projection.adjustments.get(adjustment_id)
        if adj is None:
            raise ValueError(f"差异单不存在：{adjustment_id}")
        if adj["status"] != "requested":
            raise E.BusinessRuleError(E.ADJUSTMENT_NOT_APPROVED,
                                      f"差异单已 {adj['status']}，不可重复审批")
        etype = "adjustment.approved" if approve else "adjustment.rejected"
        return self._emit(etype, adjustment_id, occurred_at,
                          {"approver": approver, "reason": reason})

    def apply_adjustment(self, adjustment_id: str, occurred_at: str | datetime,
                         *, actor: str | None = None) -> dict[str, Any]:
        """执行已批准差异单。

        关键判定：occurred_at（改单实际发生的业务时间）晚于该波封签时间的，
        一律拒绝——封签后的部分不能再被差异单改变。时间先后即事实先后。
        """
        adj = self.projection.adjustments.get(adjustment_id)
        if adj is None:
            raise E.BusinessRuleError(E.ADJUSTMENT_NOT_APPROVED,
                                      f"差异单不存在或未获批：{adjustment_id}")
        if adj["status"] != "approved":
            raise E.BusinessRuleError(
                E.ADJUSTMENT_NOT_APPROVED,
                f"差异单状态为 {adj['status']}，只有已批准差异单可以改单",
                adjustment_id=adjustment_id, status=adj["status"],
            )
        wave = self._wave(adj["wave_id"])
        t = parse_dt(occurred_at)
        sealed_at = wave.get("sealed_at")
        if sealed_at is not None and t >= sealed_at and wave["state"] != "transferred":
            raise E.BusinessRuleError(
                E.ADJUSTMENT_TARGET_SEALED,
                f"差异单业务时间 {iso(t)} 晚于封签时间 {iso(sealed_at)}，封签后不得变更",
                adjustment_id=adjustment_id, wave_id=wave["wave_id"],
                sealed_at=iso(sealed_at), occurred_at=iso(t),
            )
        if adj["kind"] == "remove_container":
            cid = adj["detail"]["container_id"]
            c = self.projection.containers.get(cid)
            if c is None:
                raise ValueError(f"容器不存在：{cid}")
            if c["state"] in ("sealed", "signed", "returned"):
                raise E.BusinessRuleError(
                    E.ADJUSTMENT_TARGET_SEALED,
                    f"容器 {cid} 已 {c['state']}，不能从本波摘除",
                    container_id=cid, state=c["state"],
                )
        new_version = f"{wave['wave_id']}@v{wave['version_seq'] + 1}"
        return self._emit(
            "adjustment.applied", adjustment_id, t,
            {"wave_id": wave["wave_id"], "new_delivery_version": new_version,
             "version_seq": wave["version_seq"] + 1, "actor": actor},
            delivery_version=new_version,
        )

    # --------------------------------------------------------------- 领单 / 装箱 / 封签

    def assign_driver(self, wave_id: str, driver_id: str,
                      occurred_at: str | datetime) -> dict[str, Any]:
        """司机领单：以当前配送版本出单。差异单升级版本后必须重新领单。"""
        wave = self._wave(wave_id)
        t = parse_dt(occurred_at)
        if wave["state"] not in ("cutoff",):
            raise E.BusinessRuleError(
                E.WAVE_CLOSED,
                f"波次 {wave_id} 状态 {wave['state']}，仅截单后可领单",
                wave_id=wave_id, state=wave["state"],
            )
        if t < wave["cutoff_at"]:
            raise E.BusinessRuleError(E.SCAN_OUT_OF_ORDER, "领单时间早于截单时间")
        return self._emit(
            "driver.assigned", wave_id, t,
            {"driver_id": driver_id, "delivery_version": wave["delivery_version"]},
            delivery_version=wave["delivery_version"],
        )

    def load_container(self, scan_id: str, container_id: str,
                       occurred_at: str | datetime, *, actor: str | None = None,
                       wave_id: str | None = None) -> dict[str, Any]:
        """仓库装箱扫描。跨线路箱、重复装车、封签后补扫一律拒绝。

        wave_id 为装车门位/司机清单上下文：扫到的箱不属于该波（属于另一条
        线路的清单）时在此拦截，避免“到门店才发现”。
        """
        seen = self.store.seen_scan(scan_id)
        if seen is not None:
            return {"scan_id": scan_id, "duplicate": True,
                    "effect": seen["event_type"], "aggregate_id": seen["aggregate_id"]}

        c = self.projection.containers.get(container_id)
        if c is None:
            raise ValueError(f"容器不存在：{container_id}")
        planned_wave_id = c.get("wave_id")
        if planned_wave_id is None:
            raise E.BusinessRuleError(E.CONTAINER_ROUTE_MISMATCH,
                                      f"容器 {container_id} 尚未编入任何波次",
                                      container_id=container_id)
        if wave_id is not None and wave_id != planned_wave_id:
            other = self.projection.waves.get(planned_wave_id)
            raise E.BusinessRuleError(
                E.CONTAINER_ROUTE_MISMATCH,
                f"容器 {container_id} 属于波次 {planned_wave_id}"
                f"（线路 {c['route_id']}），不在本波 {wave_id} 的装车清单内",
                container_id=container_id, container_route=c["route_id"],
                expected_wave=wave_id, actual_wave=planned_wave_id,
                actual_route=other["route_id"] if other else None,
            )
        wave = self._wave(planned_wave_id)
        t = parse_dt(occurred_at)

        # 跨线路：清单容器属于另一条线路 —— 在装车口直接拦下
        if c["route_id"] != wave["route_id"]:
            raise E.BusinessRuleError(
                E.CONTAINER_ROUTE_MISMATCH,
                f"容器 {container_id} 属于线路 {c['route_id']}，与本波线路 {wave['route_id']} 不符",
                container_id=container_id,
                container_route=c["route_id"], wave_route=wave["route_id"],
            )

        # 业务时间校验：离线补传以发生时间判定，与上传时的当前状态无关
        cutoff_at = wave.get("cutoff_at")
        if cutoff_at is None:
            raise E.BusinessRuleError(
                E.WAVE_CLOSED, f"波次 {planned_wave_id} 尚未截单，不能装箱",
                wave_id=planned_wave_id,
            )
        if t < cutoff_at:
            raise E.BusinessRuleError(E.SCAN_OUT_OF_ORDER, "装车扫描早于截单时间")
        sealed_at = wave.get("sealed_at")
        if sealed_at is not None and t >= sealed_at:
            raise E.BusinessRuleError(
                E.SCAN_OUT_OF_ORDER,
                f"装车扫描业务时间 {iso(t)} 不早于封签时间 {iso(sealed_at)}，封签后装车无效",
                container_id=container_id, sealed_at=iso(sealed_at),
                occurred_at=iso(t),
            )
        # 新 scan_id 但容器在该业务时间已经装车：重复扫描不得再次装车
        if c.get("loaded_at") is not None and c["loaded_at"] <= t:
            raise E.BusinessRuleError(
                E.DUPLICATE_SCAN, f"容器 {container_id} 已装车，重复扫描不产生第二遍效果",
                container_id=container_id, state=c["state"],
            )

        self.store.remember_scan(scan_id, "container.loaded", container_id, iso(t))
        result = self._emit(
            "container.loaded", container_id, t,
            {"scan_id": scan_id, "wave_id": planned_wave_id,
             "vehicle_id": wave["vehicle_id"], "actor": actor},
            delivery_version=wave["delivery_version"],
        )
        result["scan_id"] = scan_id
        result["duplicate"] = False
        return result

    def seal_vehicle(self, wave_id: str, driver_id: str,
                     occurred_at: str | datetime) -> dict[str, Any]:
        """车辆封签：并发封签只留一个结果（数据库唯一约束 + 状态校验）。"""
        wave = self._wave(wave_id)
        t = parse_dt(occurred_at)
        if wave["state"] in ("sealed", "transferred", "delivered", "returned"):
            raise E.BusinessRuleError(
                E.ALREADY_SEALED, f"波次 {wave_id} 已封签（{wave['state']}），不能重复封签",
                wave_id=wave_id, state=wave["state"],
            )
        if wave["state"] != "loading":
            raise E.BusinessRuleError(
                E.WAVE_CLOSED,
                f"波次 {wave_id} 当前 {wave['state']}，须领单并完成装箱后才能封签",
                wave_id=wave_id, state=wave["state"],
            )
        if wave["driver_id"] != driver_id or wave["driver_version"] != wave["delivery_version"]:
            raise E.BusinessRuleError(
                E.VERSION_MISMATCH,
                "领单司机或配送版本不一致，请按最新版本重新领单后再封签",
                wave_id=wave_id, expected_driver=wave["driver_id"],
                got_driver=driver_id, current_version=wave["delivery_version"],
            )
        pending = [a["container_id"] for a in wave["assignments"]
                   if self.projection.containers[a["container_id"]]["state"] != "loaded"]
        if pending:
            raise E.BusinessRuleError(
                E.SCAN_OUT_OF_ORDER, f"尚有容器未完成装箱：{pending}",
                wave_id=wave_id, pending=pending,
            )
        seal_event_id = _new_id("evt")
        if not self.store.try_seal(
            wave["delivery_version"], wave_id, wave["vehicle_id"],
            driver_id, seal_event_id, iso(t),
        ):
            winner = self.store.seal_of(wave["delivery_version"])
            raise E.BusinessRuleError(
                E.CONCURRENT_SEAL,
                f"配送版本 {wave['delivery_version']} 已被封签，并发封签仅保留一个结果",
                wave_id=wave_id, winner_event_id=winner["seal_event_id"] if winner else None,
            )
        try:
            result = self._emit(
                "vehicle.sealed", wave_id, t,
                {"driver_id": driver_id, "vehicle_id": wave["vehicle_id"]},
                delivery_version=wave["delivery_version"],
                event_id=seal_event_id,
            )
        except Exception:
            self.store.rollback()
            self._rebuild()
            raise
        result["delivery_version"] = wave["delivery_version"]
        return result

    # --------------------------------------------------------------- 门店签收

    def sign_container(self, scan_id: str, container_id: str, store_id: str,
                       occurred_at: str | datetime, *, actor: str | None = None) -> dict[str, Any]:
        seen = self.store.seen_scan(scan_id)
        if seen is not None:
            return {"scan_id": scan_id, "duplicate": True,
                    "effect": seen["event_type"], "aggregate_id": seen["aggregate_id"]}

        c = self.projection.containers.get(container_id)
        if c is None:
            raise ValueError(f"容器不存在：{container_id}")
        wave_id = c.get("wave_id")
        if wave_id is None:
            raise E.BusinessRuleError(E.CONTAINER_ROUTE_MISMATCH,
                                      f"容器 {container_id} 无配送波次")
        wave = self._wave(wave_id)
        t = parse_dt(occurred_at)

        if c["route_id"] != wave["route_id"]:
            raise E.BusinessRuleError(E.CONTAINER_ROUTE_MISMATCH,
                                      "签收容器与波次线路不符")
        if c["state"] not in ("sealed",):
            if c["state"] == "signed":
                raise E.BusinessRuleError(
                    E.DUPLICATE_SCAN, f"容器 {container_id} 已签收，不得重复签收",
                    container_id=container_id, signed_at=iso(c["signed_at"]),
                )
            raise E.BusinessRuleError(
                E.SCAN_OUT_OF_ORDER,
                f"容器 {container_id} 当前 {c['state']}，须随车封签后才能签收",
                container_id=container_id, state=c["state"],
            )
        if c["store_id"] != store_id:
            raise E.BusinessRuleError(
                E.CONTAINER_ROUTE_MISMATCH,
                f"容器 {container_id} 不属于门店 {store_id}",
                container_id=container_id, expected_store=c["store_id"],
                got_store=store_id,
            )
        if t < wave["sealed_at"]:
            raise E.BusinessRuleError(E.SCAN_OUT_OF_ORDER,
                                      "签收时间早于封签时间，按业务时间判定无效")

        self.store.remember_scan(scan_id, "store.signed", container_id, iso(t))
        result = self._emit(
            "store.signed", container_id, t,
            {"scan_id": scan_id, "wave_id": wave_id, "store_id": store_id,
             "vehicle_id": wave["vehicle_id"], "actor": actor},
            delivery_version=wave["delivery_version"],
        )
        result["scan_id"] = scan_id
        result["duplicate"] = False
        return result

    def return_container(self, scan_id: str, container_id: str,
                         occurred_at: str | datetime, *, actor: str | None = None) -> dict[str, Any]:
        """箱筐回收扫描：未签收的容器不能回收；重复回收幂等拒绝二遍效果。"""
        seen = self.store.seen_scan(scan_id)
        if seen is not None:
            return {"scan_id": scan_id, "duplicate": True,
                    "effect": seen["event_type"], "aggregate_id": seen["aggregate_id"]}
        c = self.projection.containers.get(container_id)
        if c is None:
            raise ValueError(f"容器不存在：{container_id}")
        t = parse_dt(occurred_at)
        if c["state"] != "signed":
            raise E.BusinessRuleError(
                E.SCAN_OUT_OF_ORDER,
                f"容器 {container_id} 当前 {c['state']}，签收后方可回收",
                container_id=container_id, state=c["state"],
            )
        if t < c["signed_at"]:
            raise E.BusinessRuleError(E.SCAN_OUT_OF_ORDER, "回收扫描早于签收时间")
        self.store.remember_scan(scan_id, "container.returned", container_id, iso(t))
        result = self._emit(
            "container.returned", container_id, t,
            {"scan_id": scan_id, "actor": actor},
        )
        result["scan_id"] = scan_id
        result["duplicate"] = False
        return result

    # --------------------------------------------------------------- 缺货 / 延迟告知

    def report_shortage(self, order_id: str, sku: str, expected_qty: int, actual_qty: int,
                        reason: str, occurred_at: str | datetime, *,
                        wave_id: str | None = None, container_id: str | None = None) -> dict[str, Any]:
        if order_id not in self.projection.orders:
            raise ValueError(f"订单不存在：{order_id}")
        if reason not in (E.SUPPLY_SHORT, E.NOT_LOADED, E.REMOVED_BY_ADJUSTMENT):
            raise ValueError(f"缺货原因码非法：{reason}")
        return self._emit(
            "delivery.shortage", order_id, occurred_at,
            {"sku": sku, "expected_qty": expected_qty, "actual_qty": actual_qty,
             "reason": reason, "wave_id": wave_id, "container_id": container_id},
        )

    def report_delay(self, wave_id: str, reason: str, occurred_at: str | datetime,
                     *, new_eta: str | None = None, detail: str | None = None) -> dict[str, Any]:
        self._wave(wave_id)
        if reason not in (E.VEHICLE_BREAKDOWN, E.REASSIGNMENT, E.WAVE_REPLAN):
            raise ValueError(f"延迟原因码非法：{reason}")
        return self._emit(
            "delivery.delayed", wave_id, occurred_at,
            {"reason": reason, "new_eta": new_eta, "detail": detail},
        )

    # --------------------------------------------------------------- 故障与整车转派

    def report_breakdown(self, vehicle_id: str, occurred_at: str | datetime,
                         *, reason: str = E.VEHICLE_BREAKDOWN) -> dict[str, Any]:
        v = self.projection.vehicles.get(vehicle_id)
        if v is None:
            raise ValueError(f"车辆不存在：{vehicle_id}")
        if v["status"] == "broken":
            return {"vehicle_id": vehicle_id, "duplicate": True}
        wave_id = v.get("wave_id")
        result = self._emit("vehicle.breakdown_reported", vehicle_id, occurred_at,
                            {"reason": reason, "wave_id": wave_id})
        if wave_id is not None and self.projection.waves[wave_id]["state"] == "sealed":
            self._emit("delivery.delayed", wave_id, occurred_at,
                       {"reason": E.VEHICLE_BREAKDOWN,
                        "detail": f"车辆 {vehicle_id} 故障，等待转派"})
        return result

    def reassign_vehicle(self, wave_id: str, new_vehicle_id: str, driver_id: str,
                         occurred_at: str | datetime) -> dict[str, Any]:
        """整车转派：重校载重/温层/时长/窗口；已交接容器逐件跟随并留痕。"""
        wave = self._wave(wave_id)
        t = parse_dt(occurred_at)
        old = self.projection.vehicles.get(wave["vehicle_id"])
        if old is None:
            raise ValueError("原车不存在")
        if wave["state"] != "sealed":
            raise E.BusinessRuleError(
                E.NOTHING_TO_REASSIGN,
                f"波次 {wave_id} 当前 {wave['state']}，仅已封签在途车辆故障可整车转派",
                wave_id=wave_id, state=wave["state"],
            )
        if old["status"] != "broken":
            raise E.BusinessRuleError(
                E.VEHICLE_NOT_BROKEN,
                f"原车 {old['vehicle_id']} 未报告故障，不允许转派",
                vehicle_id=old["vehicle_id"],
            )
        new = self.projection.vehicles.get(new_vehicle_id)
        if new is None:
            raise ValueError(f"接替车辆不存在：{new_vehicle_id}")
        if new["status"] != "active" or new.get("wave_id"):
            raise E.BusinessRuleError(
                E.REASSIGNMENT_CONSTRAINT_FAILED,
                f"接替车 {new_vehicle_id} 不可用（状态 {new['status']}）",
                vehicle_id=new_vehicle_id,
            )

        self._check_reassignment_constraints(wave, new)

        new_version = f"{wave['wave_id']}@v{wave['version_seq'] + 1}"
        # 与封签相同的原子抢占：并发转派只留一个结果
        if not self.store.try_seal(
            new_version, wave_id, new_vehicle_id, driver_id,
            _new_id("evt"), iso(t),
        ):
            raise E.BusinessRuleError(E.CONCURRENT_SEAL,
                                      f"版本 {new_version} 已存在转派结果")
        payload = {
            "old_vehicle_id": old["vehicle_id"],
            "new_vehicle_id": new_vehicle_id,
            "driver_id": driver_id,
            "new_delivery_version": new_version,
            "version_seq": wave["version_seq"] + 1,
            "container_ids": [a["container_id"] for a in wave["assignments"]],
        }
        try:
            result = self._emit("vehicle.reassigned", wave_id, t, payload,
                                delivery_version=new_version, commit=False)
            self._emit("delivery.delayed", wave_id, t,
                       {"reason": E.REASSIGNMENT,
                        "detail": f"{old['vehicle_id']} -> {new_vehicle_id}"},
                       commit=False)
            self.store.commit()
        except Exception:
            self.store.rollback()
            self._rebuild()
            raise
        result["delivery_version"] = new_version
        return result

    def _check_reassignment_constraints(self, wave: dict, new_vehicle: dict) -> None:
        zones: set[str] = set()
        weight = 0.0
        tags: set[str] = set()
        conflicts: set[str] = set()
        for a in wave["assignments"]:
            for line in a["items"]:
                zones.add(line["zone"])
                weight += line["weight"] * line["qty"]
                tags.update(line.get("coload_tags", []))
                conflicts.update(line.get("conflicts", []))
        tare = sum(t.get("container_count", 0)
                   for t in wave["return_tasks"]) * CONTAINER_TARE_KG
        weight += tare
        problems: list[str] = []
        if not zones <= new_vehicle["zones"]:
            problems.append("ZONE_LIMIT")
        if weight > new_vehicle["capacity_kg"]:
            problems.append("CAPACITY_LIMIT")
        if conflicts & tags:
            problems.append("COLOADING_CONFLICT")
        stores = {a["store_id"] for a in wave["assignments"]}
        stores.update(t["store_id"] for t in wave["return_tasks"])
        order_ids = {a["order_id"] for a in wave["assignments"]}
        service = max(
            (self.projection.orders[o]["service_minutes"]
             for o in order_ids if o in self.projection.orders),
            default=15,
        )
        duration = new_vehicle["base_minutes"].get(wave["route_id"], 0) + len(stores) * service
        if duration > new_vehicle["route_minutes_limit"]:
            problems.append("ROUTE_DURATION")
        if problems:
            raise E.BusinessRuleError(
                E.REASSIGNMENT_CONSTRAINT_FAILED,
                f"接替车 {new_vehicle['vehicle_id']} 不满足：{'、'.join(problems)}",
                reasons=problems,
            )

    # --------------------------------------------------------------- 离线批量补传

    def upload_scans(self, scans: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """离线扫描补传：按业务发生时间排序后逐条校验，重复扫描幂等。"""
        ordered = sorted(scans, key=lambda s: parse_dt(s["occurred_at"]))
        report: list[dict[str, Any]] = []
        for s in ordered:
            entry = {"scan_id": s["scan_id"], "type": s["type"],
                     "occurred_at": s["occurred_at"]}
            try:
                if s["type"] == "load":
                    r = self.load_container(s["scan_id"], s["container_id"],
                                            s["occurred_at"], actor=s.get("actor"),
                                            wave_id=s.get("wave_id"))
                elif s["type"] == "sign":
                    r = self.sign_container(s["scan_id"], s["container_id"],
                                            s["store_id"], s["occurred_at"],
                                            actor=s.get("actor"))
                elif s["type"] == "return":
                    r = self.return_container(s["scan_id"], s["container_id"],
                                              s["occurred_at"], actor=s.get("actor"))
                else:
                    raise ValueError(f"未知扫描类型：{s['type']}")
                entry["status"] = "duplicate" if r.get("duplicate") else "applied"
            except E.BusinessRuleError as exc:
                entry["status"] = "rejected"
                entry["reason"] = exc.reason
                entry["message"] = str(exc)
            report.append(entry)
        return report

    # --------------------------------------------------------------- 查询

    def _wave(self, wave_id: str) -> dict[str, Any]:
        wave = self.projection.waves.get(wave_id)
        if wave is None:
            raise ValueError(f"波次不存在：{wave_id}")
        return wave

    def wave_view(self, wave_id: str) -> dict[str, Any]:
        wave = self._wave(wave_id)
        return {
            "wave_id": wave["wave_id"],
            "route_id": wave["route_id"],
            "state": wave["state"],
            "vehicle_id": wave["vehicle_id"],
            "driver_id": wave["driver_id"],
            "delivery_version": wave["delivery_version"],
            "depart_at": wave["depart_at"],
            "cutoff_at": iso(wave["cutoff_at"]) if wave.get("cutoff_at") else None,
            "sealed_at": iso(wave["sealed_at"]) if wave.get("sealed_at") else None,
            "delivered_at": iso(wave["delivered_at"]) if wave.get("delivered_at") else None,
            "containers": [
                {"container_id": a["container_id"], "order_id": a["order_id"],
                 "store_id": a["store_id"],
                 "state": self.projection.containers[a["container_id"]]["state"]}
                for a in wave["assignments"]
            ],
            "delay_reasons": [
                {"reason": d["reason"], "at": iso(d["at"]), "new_eta": d.get("new_eta")}
                for d in wave["delay_reasons"]
            ],
            "shortages": [
                {"order_id": s["order_id"], "sku": s["sku"], "reason": s["reason"],
                 "expected_qty": s["expected_qty"], "actual_qty": s["actual_qty"]}
                for s in wave["shortages"]
            ],
        }

    def container_trace(self, container_id: str) -> dict[str, Any]:
        """容器逐件追踪：完整交接链。"""
        c = self.projection.containers.get(container_id)
        if c is None:
            raise ValueError(f"容器不存在：{container_id}")
        return {
            "container_id": container_id,
            "route_id": c["route_id"],
            "state": c["state"],
            "wave_id": c["wave_id"],
            "vehicle_id": c["vehicle_id"],
            "order_id": c["order_id"],
            "store_id": c["store_id"],
            "custody": [
                {"at": iso(x["at"]), **{k: v for k, v in x.items() if k != "at"}}
                for x in c["custody"]
            ],
        }

    def store_status(self, store_id: str) -> dict[str, Any]:
        """门店视角：为什么缺货、为什么拆单、为什么延迟，逐单逐箱可查。"""
        orders_out: list[dict[str, Any]] = []
        p = self.projection
        for o in p.orders.values():
            if o["store_id"] != store_id:
                continue
            waves = [self.wave_view(w) for w in o["wave_ids"]]
            orders_out.append({
                "order_id": o["order_id"],
                "active": o["active"],
                "merged_into": o["merged_into"],
                "parent": o["parent"],
                "shortages": [
                    {"sku": s["sku"], "reason": s["reason"],
                     "expected_qty": s["expected_qty"], "actual_qty": s["actual_qty"],
                     "wave_id": s["wave_id"]}
                    for s in o.get("shortages", [])
                ],
                "split_reasons": [
                    {"reason": s["reason"], "child": s.get("child"),
                     "wave_id": s.get("wave_id")}
                    for s in o["split_reasons"] if "child" in s
                ],
                "waves": [
                    {
                        "wave_id": w["wave_id"], "state": w["state"],
                        "delivery_version": w["delivery_version"],
                        "vehicle_id": w["vehicle_id"],
                        "delays": w["delay_reasons"],
                    }
                    for w in waves
                ],
            })
        containers = [
            self.container_trace(cid) for cid, c in p.containers.items()
            if c["store_id"] == store_id
        ]
        return {"store_id": store_id, "orders": orders_out, "containers": containers}

    def wave_timeline(self, wave_id: str) -> list[dict[str, Any]]:
        """仓库回看：按业务发生时间列出该波次的关键节点。

        改单（adjustment.applied）与封签（vehicle.sealed）的先后，
        直接看 occurred_at 即可判定，不再依赖人工回忆。
        """
        self._wave(wave_id)
        milestone = {
            "wave.planned", "wave.cutoff", "driver.assigned",
            "container.loaded", "container.unloaded", "vehicle.sealed",
            "adjustment.applied", "vehicle.reassigned",
            "store.signed", "container.returned",
            "delivery.delayed",
        }
        rows = []
        for row in self.store.all_events():
            if row["event_type"] not in milestone:
                continue
            belongs = row["aggregate_id"] == wave_id
            if not belongs:
                payload = json.loads(row["payload"])
                container = self.projection.containers.get(row["aggregate_id"])
                belongs = (
                    payload.get("wave_id") == wave_id
                    or (row["delivery_version"] or "").startswith(f"{wave_id}@")
                    or (container is not None and container.get("wave_id") == wave_id)
                )
            if not belongs:
                continue
            rows.append({
                "occurred_at": row["occurred_at"],
                "recorded_at": row["recorded_at"],
                "event_type": row["event_type"],
                "aggregate_id": row["aggregate_id"],
                "delivery_version": row["delivery_version"],
            })
        rows.sort(key=lambda r: (r["occurred_at"], r["event_type"]))
        return rows

    def pending_recovery(self) -> dict[str, Any]:
        """恢复运行后巡检：待转派车辆与未回收容器必须仍在正确环节。"""
        p = self.projection
        broken = [
            {"vehicle_id": v["vehicle_id"], "wave_id": v.get("wave_id"),
             "breakdown_at": iso(v["breakdown_at"])}
            for v in p.vehicles.values()
            if v["status"] == "broken" and v.get("wave_id")
        ]
        unreturned = [
            {"container_id": cid, "state": c["state"], "wave_id": c.get("wave_id"),
             "vehicle_id": c.get("vehicle_id"), "store_id": c.get("store_id")}
            for cid, c in p.containers.items()
            if c["state"] in ("signed", "sealed", "loaded", "planned", "transferred")
        ]
        in_flight = [
            self.wave_view(w["wave_id"]) for w in p.waves.values()
            if w["state"] in ("sealed", "transferred", "loading", "cutoff", "delivered")
        ]
        return {"awaiting_reassignment": broken,
                "unreturned_containers": unreturned,
                "waves": in_flight}
