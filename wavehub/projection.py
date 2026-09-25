"""读模型：把只追加事件重放成当前状态。

投影本身不做业务校验——事件在被接受时已经过业务时间校验；
崩溃恢复时整体重放即可还原：待转派车辆、未回收容器自然停在正确环节。
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .clock import parse_dt


def _items_from_payload(raw: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for line in raw:
        sku = line["sku"]
        if sku in items:
            items[sku]["qty"] += line["qty"]
        else:
            items[sku] = dict(line)
    return items


class Projection:
    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.vehicles: dict[str, dict[str, Any]] = {}
        self.waves: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.adjustments: dict[str, dict[str, Any]] = {}
        self.return_tasks: dict[str, list[dict[str, Any]]] = {}
        self.event_ids: set[str] = set()

    # ------------------------------------------------------------ 重放

    def load(self, rows: Iterable[Any]) -> None:
        for row in rows:
            self.apply(
                row["event_type"],
                row["aggregate_id"],
                json.loads(row["payload"]),
                occurred_at=row["occurred_at"],
                delivery_version=row["delivery_version"],
                event_id=row["event_id"],
            )

    def apply(self, event_type: str, aggregate_id: str, p: dict[str, Any],
              *, occurred_at: str, delivery_version: str | None = None,
              event_id: str | None = None) -> None:
        if event_id is not None:
            self.event_ids.add(event_id)
        handler = getattr(self, f"_on_{event_type.replace('.', '_')}", None)
        if handler is None:
            raise ValueError(f"投影无法识别事件：{event_type}")
        handler(aggregate_id, p, parse_dt(occurred_at), delivery_version)

    # ------------------------------------------------------------ 订单

    def _on_order_received(self, oid: str, p: dict[str, Any], t, dv) -> None:
        self.orders[oid] = {
            "order_id": oid,
            "store_id": p["store_id"],
            "route_id": p["route_id"],
            "window_open": p["window_open"],
            "window_close": p["window_close"],
            "service_minutes": p.get("service_minutes", 15),
            "items": _items_from_payload(p["items"]),
            "active": True,
            "merged_into": None,
            "parent": None,
            "children": [],
            "split_reasons": [],
            "wave_ids": [],
            "received_at": t,
        }

    def _on_order_merged(self, oid: str, p: dict[str, Any], t, dv) -> None:
        survivor = self.orders[oid]
        absorbed = self.orders[p["absorbed_order_id"]]
        for sku, line in absorbed["items"].items():
            if sku in survivor["items"]:
                survivor["items"][sku]["qty"] += line["qty"]
            else:
                survivor["items"][sku] = dict(line)
        absorbed["active"] = False
        absorbed["merged_into"] = oid
        survivor["children"].append(p["absorbed_order_id"])
        # 若两单已进入同一未截单波次，把被并订单的装车明细并入存活单的容器
        wave_id = next((w for w in absorbed.get("wave_ids", [])), None)
        if wave_id is not None:
            wave = self.waves[wave_id]
            dst = next((a for a in wave["assignments"] if a["order_id"] == oid), None)
            src = next((a for a in wave["assignments"]
                        if a["order_id"] == p["absorbed_order_id"]), None)
            if dst is not None and src is not None:
                dst["items"].extend(src["items"])
                wave["assignments"].remove(src)
                absorbed["wave_ids"].remove(wave_id)
                wave["order_ids"].remove(p["absorbed_order_id"])
                absorbed["merged_container_id"] = src["container_id"]
                mc = self.containers[src["container_id"]]
                mc.update({"state": "unloaded", "order_id": None, "store_id": None,
                           "wave_id": None, "vehicle_id": None})

    def _on_return_task_received(self, key: str, p: dict[str, Any], t, dv) -> None:
        task = {"task_id": key, "store_id": p["store_id"], "route_id": p["route_id"],
                "container_count": p["container_count"], "at": t, "planned_wave": None}
        self.return_tasks.setdefault(p["route_id"], []).append(task)

    def _on_order_split(self, oid: str, p: dict[str, Any], t, dv) -> None:
        parent = self.orders[oid]
        child_id = p["child_order_id"]
        moved = _items_from_payload(p["items"])
        for sku, line in moved.items():
            parent["items"][sku]["qty"] -= line["qty"]
            if parent["items"][sku]["qty"] <= 0:
                del parent["items"][sku]
        child = {
            "order_id": child_id,
            "store_id": parent["store_id"],
            "route_id": parent["route_id"],
            "window_open": parent["window_open"],
            "window_close": parent["window_close"],
            "service_minutes": parent["service_minutes"],
            "items": moved,
            "active": True,
            "merged_into": None,
            "parent": oid,
            "children": [],
            "split_reasons": [{"reason": p["reason"], "wave_id": p.get("wave_id"), "at": t}],
            "wave_ids": [],
            "received_at": t,
        }
        self.orders[child_id] = child
        parent["children"].append(child_id)
        parent["split_reasons"].append(
            {"reason": p["reason"], "child": child_id, "wave_id": p.get("wave_id"), "at": t}
        )

    # ------------------------------------------------------------ 车辆

    def _on_vehicle_registered(self, vid: str, p: dict[str, Any], t, dv) -> None:
        self.vehicles[vid] = {
            "vehicle_id": vid,
            "plate": p["plate"],
            "capacity_kg": p["capacity_kg"],
            "zones": frozenset(p["zones"]),
            "route_minutes_limit": p["route_minutes_limit"],
            "base_minutes": dict(p.get("base_minutes", {})),
            "status": "active",
            "wave_id": None,
        }

    def _on_vehicle_breakdown_reported(self, vid: str, p: dict[str, Any], t, dv) -> None:
        self.vehicles[vid]["status"] = "broken"
        self.vehicles[vid]["breakdown_at"] = t
        self.vehicles[vid]["breakdown_reason"] = p.get("reason", "VEHICLE_BREAKDOWN")

    # ------------------------------------------------------------ 容器

    def _on_container_registered(self, cid: str, p: dict[str, Any], t, dv) -> None:
        self.containers[cid] = {
            "container_id": cid,
            "route_id": p["route_id"],
            "zone": p.get("zone"),
            "state": "registered",
            "wave_id": None,
            "order_id": None,
            "vehicle_id": None,
            "store_id": None,
            "loaded_at": None,
            "signed_at": None,
            "returned_at": None,
            "custody": [
                {"at": t, "action": "registered", "actor": p.get("actor"),
                 "route_id": p["route_id"]}
            ],
        }

    def _plan_container(self, cid: str, assignment: dict[str, Any], wave: dict[str, Any], t) -> None:
        c = self.containers[cid]
        c.update({
            "state": "planned",
            "wave_id": wave["wave_id"],
            "order_id": assignment["order_id"],
            "vehicle_id": wave["vehicle_id"],
            "store_id": assignment.get("store_id"),
        })
        c["custody"].append(
            {"at": t, "action": "planned", "actor": "planner",
             "wave_id": wave["wave_id"], "order_id": assignment["order_id"],
             "delivery_version": wave["delivery_version"]}
        )

    def _on_wave_planned(self, wid: str, p: dict[str, Any], t, dv) -> None:
        wave = {
            "wave_id": wid,
            "route_id": p["route_id"],
            "vehicle_id": p["vehicle_id"],
            "driver_id": None,
            "driver_version": None,
            "delivery_version": p["delivery_version"],
            "version_seq": p.get("version_seq", 1),
            "state": "collecting",
            "depart_at": p["depart_at"],
            "assignments": [],          # [{container_id, order_id, store_id, items}]
            "order_ids": [],
            "return_tasks": list(p.get("return_tasks", [])),
            "cutoff_at": None,
            "sealed_at": None,
            "delivered_at": None,
            "returned_at": None,
            "plan_reasons": list(p.get("reasons", [])),
            "delay_reasons": [],
            "shortages": [],
            "planned_at": t,
            "load_weight": p.get("load_weight", 0.0),
        }
        self.waves[wid] = wave
        planned_task_ids = {t.get("task_id") for t in p.get("return_tasks", [])}
        for task in self.return_tasks.get(p["route_id"], []):
            if task.get("planned_wave") is None and task.get("task_id") in planned_task_ids:
                task["planned_wave"] = wid
        for a in p["assignments"]:
            wave["assignments"].append(dict(a))
            self._plan_container(a["container_id"], a, wave, t)
            oid = a["order_id"]
            if oid in self.orders and wid not in self.orders[oid]["wave_ids"]:
                self.orders[oid]["wave_ids"].append(wid)
                wave["order_ids"].append(oid)

    def _on_wave_cutoff(self, wid: str, p: dict[str, Any], t, dv) -> None:
        self.waves[wid]["state"] = "cutoff"
        self.waves[wid]["cutoff_at"] = t

    # ------------------------------------------------------------ 差异单

    def _on_adjustment_requested(self, aid: str, p: dict[str, Any], t, dv) -> None:
        self.adjustments[aid] = {
            "adjustment_id": aid, "wave_id": p["wave_id"],
            "kind": p["kind"], "detail": p.get("detail", {}),
            "reason": p.get("reason"), "status": "requested",
            "requested_at": t, "decided_at": None, "applied_at": None,
        }

    def _on_adjustment_approved(self, aid: str, p: dict[str, Any], t, dv) -> None:
        self.adjustments[aid]["status"] = "approved"
        self.adjustments[aid]["decided_at"] = t
        self.adjustments[aid]["approver"] = p.get("approver")

    def _on_adjustment_rejected(self, aid: str, p: dict[str, Any], t, dv) -> None:
        self.adjustments[aid]["status"] = "rejected"
        self.adjustments[aid]["decided_at"] = t
        self.adjustments[aid]["reject_reason"] = p.get("reason")

    def _on_adjustment_applied(self, aid: str, p: dict[str, Any], t, dv) -> None:
        adj = self.adjustments[aid]
        adj["status"] = "applied"
        adj["applied_at"] = t
        wave = self.waves[p["wave_id"]]
        wave["delivery_version"] = p["new_delivery_version"]
        wave["version_seq"] = p.get("version_seq", wave["version_seq"] + 1)
        # 版本升级后回到待领单：司机旧版本清单失效，必须重新领单
        wave["state"] = "cutoff"
        wave["driver_id"] = None
        wave["driver_version"] = None
        detail = adj["detail"]
        if adj["kind"] == "remove_container":
            cid = detail["container_id"]
            c = self.containers[cid]
            c["state"] = "unloaded"
            c["wave_id"] = None
            c["order_id"] = None
            c["vehicle_id"] = None
            c["store_id"] = None
            c["loaded_at"] = None
            c["custody"].append(
                {"at": t, "action": "unloaded", "actor": p.get("actor", "warehouse"),
                 "adjustment_id": aid, "wave_id": wave["wave_id"],
                 "delivery_version": p["new_delivery_version"]}
            )
            wave["assignments"] = [
                a for a in wave["assignments"] if a["container_id"] != cid
            ]
            wave["order_ids"] = [
                o for o in wave["order_ids"]
                if any(a["order_id"] == o for a in wave["assignments"])
            ]

    # ------------------------------------------------------------ 交接链

    def _on_driver_assigned(self, wid: str, p: dict[str, Any], t, dv) -> None:
        wave = self.waves[wid]
        wave["driver_id"] = p["driver_id"]
        wave["driver_version"] = p["delivery_version"]
        wave["state"] = "loading"

    def _on_container_loaded(self, cid: str, p: dict[str, Any], t, dv) -> None:
        c = self.containers[cid]
        c["state"] = "loaded"
        c["loaded_at"] = t
        c["custody"].append(
            {"at": t, "action": "loaded", "actor": p.get("actor", "warehouse"),
             "scan_id": p.get("scan_id"), "wave_id": p["wave_id"],
             "vehicle_id": p["vehicle_id"], "delivery_version": dv}
        )

    def _on_container_unloaded(self, cid: str, p: dict[str, Any], t, dv) -> None:
        c = self.containers[cid]
        c["state"] = "unloaded"
        c["loaded_at"] = None
        c["custody"].append(
            {"at": t, "action": "unloaded", "actor": p.get("actor"),
             "scan_id": p.get("scan_id"), "wave_id": p.get("wave_id"),
             "delivery_version": dv}
        )

    def _on_vehicle_sealed(self, wid: str, p: dict[str, Any], t, dv) -> None:
        wave = self.waves[wid]
        wave["state"] = "sealed"
        wave["sealed_at"] = t
        self.vehicles[wave["vehicle_id"]]["wave_id"] = wid
        for a in wave["assignments"]:
            c = self.containers[a["container_id"]]
            c["state"] = "sealed"
            c["custody"].append(
                {"at": t, "action": "sealed", "actor": p.get("driver_id"),
                 "wave_id": wid, "vehicle_id": wave["vehicle_id"],
                 "delivery_version": dv}
            )

    def _on_vehicle_reassigned(self, wid: str, p: dict[str, Any], t, dv) -> None:
        wave = self.waves[wid]
        old_vehicle = wave["vehicle_id"]
        wave["vehicle_id"] = p["new_vehicle_id"]
        wave["driver_id"] = p["driver_id"]
        wave["state"] = "transferred"
        wave["delivery_version"] = p["new_delivery_version"]
        wave["version_seq"] = p.get("version_seq", wave["version_seq"] + 1)
        wave["driver_version"] = p["new_delivery_version"]
        wave["sealed_at"] = t  # 新车在转派时重新封签
        if old_vehicle in self.vehicles:
            self.vehicles[old_vehicle]["wave_id"] = None
        self.vehicles[p["new_vehicle_id"]]["status"] = "active"
        self.vehicles[p["new_vehicle_id"]]["wave_id"] = wid
        # 容器逐件跟随整车：仅仍在车上（封签在途）的容器转移；
        # 已交接给门店（signed）的容器不动，其交接链原样保留以便逐件追踪
        for a in wave["assignments"]:
            c = self.containers[a["container_id"]]
            if c["state"] != "sealed":
                continue
            c["vehicle_id"] = p["new_vehicle_id"]
            c["custody"].append(
                {"at": t, "action": "transferred", "actor": p.get("actor", "dispatcher"),
                 "from_vehicle": old_vehicle, "to_vehicle": p["new_vehicle_id"],
                 "wave_id": wid, "delivery_version": p["new_delivery_version"]}
            )
            c["custody"].append(
                {"at": t, "action": "sealed", "actor": p["driver_id"],
                 "wave_id": wid, "vehicle_id": p["new_vehicle_id"],
                 "delivery_version": p["new_delivery_version"]}
            )

    def _on_delivery_shortage(self, oid: str, p: dict[str, Any], t, dv) -> None:
        record = {
            "order_id": oid, "sku": p["sku"],
            "expected_qty": p["expected_qty"], "actual_qty": p["actual_qty"],
            "reason": p["reason"], "wave_id": p.get("wave_id"),
            "container_id": p.get("container_id"), "at": t,
        }
        order = self.orders.get(oid)
        if order is not None:
            order.setdefault("shortages", []).append(record)
        wid = p.get("wave_id")
        if wid in self.waves:
            self.waves[wid]["shortages"].append(record)

    def _on_delivery_delayed(self, wid: str, p: dict[str, Any], t, dv) -> None:
        self.waves[wid]["delay_reasons"].append(
            {"reason": p["reason"], "detail": p.get("detail"), "at": t,
             "new_eta": p.get("new_eta")}
        )

    def _on_store_signed(self, cid: str, p: dict[str, Any], t, dv) -> None:
        c = self.containers[cid]
        c["state"] = "signed"
        c["signed_at"] = t
        c["custody"].append(
            {"at": t, "action": "signed", "actor": p.get("actor"),
             "scan_id": p.get("scan_id"), "store_id": p["store_id"],
             "wave_id": p["wave_id"], "delivery_version": dv}
        )
        wave = self.waves[p["wave_id"]]
        if all(self.containers[a["container_id"]]["state"] == "signed"
               for a in wave["assignments"]):
            wave["state"] = "delivered"
            wave["delivered_at"] = t
            if self.vehicles.get(wave["vehicle_id"], {}).get("wave_id") == p["wave_id"]:
                self.vehicles[wave["vehicle_id"]]["wave_id"] = None

    def _on_container_returned(self, cid: str, p: dict[str, Any], t, dv) -> None:
        c = self.containers[cid]
        c["state"] = "returned"
        c["returned_at"] = t
        c["custody"].append(
            {"at": t, "action": "returned", "actor": p.get("actor"),
             "scan_id": p.get("scan_id")}
        )
        wave_id = c.get("wave_id")
        if wave_id in self.waves:
            wave = self.waves[wave_id]
            if all(self.containers[a["container_id"]]["state"] == "returned"
                   for a in wave["assignments"]):
                wave["state"] = "returned"
                wave["returned_at"] = t

    # ------------------------------------------------------------ 查询

    def active_orders(self, route_id: str) -> list[dict[str, Any]]:
        return [o for o in self.orders.values()
                if o["active"] and o["route_id"] == route_id]

    def free_containers(self, route_id: str) -> list[dict[str, Any]]:
        return [c for c in self.containers.values()
                if c["route_id"] == route_id and c["state"] in ("registered", "unloaded")]

    def active_vehicles(self) -> list[dict[str, Any]]:
        return [v for v in self.vehicles.values() if v["status"] == "active"]

    def broken_vehicles(self) -> list[dict[str, Any]]:
        return [v for v in self.vehicles.values()
                if v["status"] == "broken" and v.get("wave_id")]
