"""端到端业务测试：波次编排与交接系统的全部关键规则。"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from wavehub import (
    BusinessRuleError, EventStore, Item, PlanningError, ReturnTask,
    StoreOrder, Vehicle, WaveHub,
)
from wavehub import errors as E


def item(sku: str, qty: int, zone: str = "ambient", weight: float = 10.0,
         tags=(), conflicts=()) -> Item:
    return Item(sku, qty, zone, weight, frozenset(tags), frozenset(conflicts))


def vehicle(vid: str, cap: float, zones=("ambient", "chilled", "frozen"),
            limit: int = 600, bases: dict | None = None, plate: str | None = None) -> Vehicle:
    return Vehicle(vid, plate or f"京{vid}", cap, frozenset(zones), limit, bases or {})


def order(oid: str, store: str, route: str, items, open_="2026-09-22T09:00:00+08:00",
          close="2026-09-22T12:00:00+08:00", service: int = 15) -> StoreOrder:
    return StoreOrder(oid, store, route, tuple(items), open_, close, service)


class FlowTest(unittest.TestCase):
    """领单 → 装箱 → 封签 → 签收 → 回收 主链路。"""

    def setUp(self) -> None:
        self.hub = WaveHub(":memory:")
        h = self.hub
        h.register_vehicle(vehicle("v1", 500, bases={"A": 30}), "2026-09-22T06:00:00+08:00")
        h.register_order(order("o1", "S1", "A", [item("sku-a", 2, weight=20),
                                                 item("sku-b", 1, "chilled", 30)]),
                         "2026-09-22T06:10:00+08:00")
        h.register_order(order("o2", "S2", "A", [item("sku-c", 1, "frozen", 40)]),
                         "2026-09-22T06:10:00+08:00")
        for cid in ("box-a1", "box-a2"):
            h.register_container(cid, "A", "2026-09-22T06:20:00+08:00")
        h.receive_return_task(ReturnTask("S99", "A", 5), "2026-09-22T06:30:00+08:00")
        plan = h.plan_waves("2026-09-22T07:00:00+08:00")
        self.wave_id = plan["waves"][0]["wave_id"]

    def test_full_handoff_chain_on_one_version(self) -> None:
        h = self.hub
        wid = self.wave_id
        h.cutoff_wave(wid, "2026-09-22T07:30:00+08:00")
        h.assign_driver(wid, "driver-zhang", "2026-09-22T07:40:00+08:00")
        r1 = h.load_container("scan-1", "box-a1", "2026-09-22T07:50:00+08:00")
        r2 = h.load_container("scan-2", "box-a2", "2026-09-22T08:05:00+08:00")
        self.assertFalse(r1["duplicate"])
        self.assertFalse(r2["duplicate"])

        # 重复扫描：同一 scan_id 幂等；换新 scan_id 再扫已装车箱也不得二次装车
        dup = h.load_container("scan-1", "box-a1", "2026-09-22T08:06:00+08:00")
        self.assertTrue(dup["duplicate"])
        with self.assertRaises(BusinessRuleError) as cm:
            h.load_container("scan-1b", "box-a1", "2026-09-22T08:07:00+08:00")
        self.assertEqual(cm.exception.reason, E.DUPLICATE_SCAN)

        sealed = h.seal_vehicle(wid, "driver-zhang", "2026-09-22T08:20:00+08:00")
        self.assertEqual(sealed["delivery_version"], f"{wid}@v1")
        self.assertEqual(h.wave_view(wid)["state"], "sealed")

        # 封签后补扫装车：按业务时间判定无效
        with self.assertRaises(BusinessRuleError) as cm:
            h.load_container("scan-late", "box-a1", "2026-09-22T09:00:00+08:00")
        self.assertEqual(cm.exception.reason, E.SCAN_OUT_OF_ORDER)

        # 签收：错门店拒收；重复签收无效
        with self.assertRaises(BusinessRuleError) as cm:
            h.sign_container("scan-sx", "box-a1", "S2", "2026-09-22T10:00:00+08:00")
        self.assertEqual(cm.exception.reason, E.CONTAINER_ROUTE_MISMATCH)
        h.sign_container("scan-s1", "box-a1", "S1", "2026-09-22T10:00:00+08:00")
        with self.assertRaises(BusinessRuleError) as cm:
            h.sign_container("scan-s1-again", "box-a1", "S1",
                             "2026-09-22T10:05:00+08:00")
        self.assertEqual(cm.exception.reason, E.DUPLICATE_SCAN)
        # 同 scan_id 重传签收：幂等返回
        again = h.sign_container("scan-s1", "box-a1", "S1",
                                 "2026-09-22T10:06:00+08:00")
        self.assertTrue(again["duplicate"])

        h.sign_container("scan-s2", "box-a2", "S2", "2026-09-22T10:20:00+08:00")
        self.assertEqual(h.wave_view(wid)["state"], "delivered")

        # 回收任务的箱筐回收
        h.return_container("scan-r1", "box-a1", "2026-09-22T11:00:00+08:00")
        h.return_container("scan-r2", "box-a2", "2026-09-22T11:10:00+08:00")
        self.assertEqual(h.wave_view(wid)["state"], "returned")

        # 容器逐件交接链完整
        trace = h.container_trace("box-a1")
        actions = [c["action"] for c in trace["custody"]]
        self.assertEqual(
            actions,
            ["registered", "planned", "loaded", "sealed", "signed", "returned"],
        )

    def test_cross_route_container_rejected_at_loading_dock(self) -> None:
        """清单里的周转箱属于另一条线路：在装车口即拦截，而不是到门店才发现。"""
        h = self.hub
        # 搭一条 B 线路并编排，让 box-b1 属于 B 波
        h.register_vehicle(vehicle("vb", 300, bases={"B": 20}),
                           "2026-09-22T06:00:00+08:00")
        h.register_order(order("ob", "SB", "B", [item("sku-x", 1)]),
                         "2026-09-22T06:10:00+08:00")
        h.register_container("box-b1", "B", "2026-09-22T06:20:00+08:00")
        h.plan_waves("2026-09-22T07:00:00+08:00")
        wid = self.wave_id
        h.cutoff_wave(wid, "2026-09-22T07:30:00+08:00")
        h.assign_driver(wid, "driver-zhang", "2026-09-22T07:40:00+08:00")
        with self.assertRaises(BusinessRuleError) as cm:
            h.load_container("scan-wrong", "box-b1", "2026-09-22T07:55:00+08:00",
                             wave_id=wid)
        self.assertEqual(cm.exception.reason, E.CONTAINER_ROUTE_MISMATCH)
        self.assertEqual(cm.exception.context["container_route"], "B")
        self.assertEqual(cm.exception.context["actual_wave"], "wave-B-01")


class PlanningTest(unittest.TestCase):
    def plan(self, hub: WaveHub) -> dict:
        return hub.plan_waves("2026-09-22T07:00:00+08:00")

    def test_zone_constraint_picks_qualified_vehicle(self) -> None:
        hub = WaveHub(":memory:")
        # 冷藏车不能运冷冻，另有一台冷冻车
        hub.register_vehicle(vehicle("v-chill", 500, zones=("ambient", "chilled"),
                                     bases={"A": 30}), "2026-09-22T06:00:00+08:00")
        hub.register_vehicle(vehicle("v-freeze", 500,
                                     zones=("ambient", "chilled", "frozen"),
                                     bases={"A": 30}), "2026-09-22T06:01:00+08:00")
        hub.register_order(order("o1", "S1", "A", [item("sku-f", 1, "frozen", 40)]),
                           "2026-09-22T06:10:00+08:00")
        hub.register_container("box-1", "A", "2026-09-22T06:20:00+08:00")
        out = self.plan(hub)
        self.assertEqual(out["waves"][0]["vehicle_id"], "v-freeze")

    def test_capacity_forces_split_with_reason(self) -> None:
        hub = WaveHub(":memory:")
        # 单车 60kg 装不下 100kg：订单拆两波，各 50kg，需要两台车
        hub.register_vehicle(vehicle("v-small-1", 60, bases={"C": 20}),
                             "2026-09-22T06:00:00+08:00")
        hub.register_vehicle(vehicle("v-small-2", 60, bases={"C": 20}),
                             "2026-09-22T06:01:00+08:00")
        hub.register_order(order("o-big", "SC", "C", [item("sku-h", 2, weight=50)]),
                           "2026-09-22T06:10:00+08:00")
        hub.register_container("box-c1", "C", "2026-09-22T06:20:00+08:00")
        hub.register_container("box-c2", "C", "2026-09-22T06:21:00+08:00")
        out = self.plan(hub)
        # 一单两波：父波 50kg，子单 50kg，原因码 CAPACITY_LIMIT
        self.assertEqual(len(out["waves"]), 2)
        self.assertEqual(len(out["splits"]), 1)
        self.assertEqual(out["splits"][0]["reason"], "CAPACITY_LIMIT")
        status = hub.store_status("SC")
        self.assertEqual(status["orders"][0]["split_reasons"][0]["reason"],
                         "CAPACITY_LIMIT")

    def test_coloading_conflict_splits_into_two_waves(self) -> None:
        hub = WaveHub(":memory:")
        hub.register_vehicle(vehicle("v1", 500, bases={"D": 20}),
                             "2026-09-22T06:00:00+08:00")
        hub.register_vehicle(vehicle("v2", 500, bases={"D": 20}),
                             "2026-09-22T06:01:00+08:00")
        hub.register_order(
            order("o-chem", "SD1", "D", [item("sku-chem", 1, tags=("chem",))]),
            "2026-09-22T06:10:00+08:00")
        hub.register_order(
            order("o-food", "SD2", "D", [item("sku-food", 1, conflicts=("chem",))]),
            "2026-09-22T06:11:00+08:00")
        hub.register_container("box-d1", "D", "2026-09-22T06:20:00+08:00")
        hub.register_container("box-d2", "D", "2026-09-22T06:21:00+08:00")
        out = self.plan(hub)
        self.assertEqual(len(out["waves"]), 2)
        all_reasons = {r for w in out["waves"] for r in w["reasons"]}
        self.assertIn("COLOADING_CONFLICT", all_reasons)
        self.assertNotEqual(out["waves"][0]["vehicle_id"], out["waves"][1]["vehicle_id"])

    def test_unreachable_window_has_no_plan(self) -> None:
        hub = WaveHub(":memory:")
        hub.register_vehicle(
            vehicle("v-far", 500, bases={"E": 300}, limit=1000),
            "2026-09-22T06:00:00+08:00")
        hub.register_order(
            order("o-far", "SE", "E", [item("sku-e", 1)],
                  open_="2026-09-22T09:00:00+08:00",
                  close="2026-09-22T09:05:00+08:00"),
            "2026-09-22T06:10:00+08:00")
        hub.register_container("box-e1", "E", "2026-09-22T06:20:00+08:00")
        with self.assertRaises(PlanningError) as cm:
            self.plan(hub)
        self.assertEqual(cm.exception.reason, E.NO_ELIGIBLE_VEHICLE)

    def test_return_task_counts_into_stops_and_tonnage(self) -> None:
        hub = WaveHub(":memory:")
        hub.register_vehicle(vehicle("v1", 500, bases={"A": 30}),
                             "2026-09-22T06:00:00+08:00")
        hub.register_order(order("o1", "S1", "A", [item("sku-a", 2, weight=20)]),
                           "2026-09-22T06:10:00+08:00")
        hub.register_container("box-a1", "A", "2026-09-22T06:20:00+08:00")
        hub.receive_return_task(ReturnTask("S99", "A", 5),
                                "2026-09-22T06:30:00+08:00")
        out = self.plan(hub)
        spec = out["waves"][0]
        # 40kg 商品 + 5 只空箱 × 2kg 皮重 = 50kg
        self.assertEqual(spec["load_weight"], 50.0)
        self.assertEqual(spec["return_tasks"][0]["store_id"], "S99")
        # 回收任务不重复占用：再编排一次不会把同一任务带上
        hub.register_order(order("o2", "S2", "A", [item("sku-b", 1)]),
                           "2026-09-22T06:40:00+08:00")
        hub.register_container("box-a2", "A", "2026-09-22T06:41:00+08:00")
        out2 = self.plan(hub)
        self.assertEqual(out2["waves"][0]["return_tasks"], [])


class CutoffAdjustmentTest(unittest.TestCase):
    """截单前合并；截单后只有获批差异单能改未封签部分。"""

    def _world(self) -> tuple[WaveHub, str]:
        hub = WaveHub(":memory:")
        hub.register_vehicle(vehicle("v1", 500, bases={"A": 30}),
                             "2026-09-22T06:00:00+08:00")
        hub.register_order(order("o1", "S1", "A", [item("sku-a", 1, weight=20)]),
                           "2026-09-22T06:10:00+08:00")
        hub.register_order(order("o2", "S1", "A", [item("sku-b", 1, weight=10)]),
                           "2026-09-22T06:11:00+08:00")
        hub.register_container("box-a1", "A", "2026-09-22T06:20:00+08:00")
        hub.register_container("box-a2", "A", "2026-09-22T06:21:00+08:00")
        out = hub.plan_waves("2026-09-22T07:00:00+08:00")
        return hub, out["waves"][0]["wave_id"]

    def test_merge_before_cutoff(self) -> None:
        hub, wid = self._world()
        hub.merge_orders("o1", "o2", "2026-09-22T07:10:00+08:00")
        view = hub.wave_view(wid)
        # 两单并入一个容器
        self.assertEqual(len(view["containers"]), 1)
        self.assertFalse(hub.projection.orders["o2"]["active"])

    def test_merge_after_cutoff_rejected(self) -> None:
        hub, wid = self._world()
        hub.cutoff_wave(wid, "2026-09-22T07:30:00+08:00")
        with self.assertRaises(BusinessRuleError) as cm:
            hub.merge_orders("o1", "o2", "2026-09-22T07:40:00+08:00")
        self.assertEqual(cm.exception.reason, E.WAVE_CLOSED)

    def test_adjustment_lifecycle_and_seal_time_boundary(self) -> None:
        hub, wid = self._world()
        hub.cutoff_wave(wid, "2026-09-22T07:30:00+08:00")

        # 截单后没有差异单直接改不动；未批准的差异单也不能执行
        with self.assertRaises(BusinessRuleError) as cm:
            hub.apply_adjustment("adj-nope", "2026-09-22T07:45:00+08:00")
        self.assertEqual(cm.exception.reason, E.ADJUSTMENT_NOT_APPROVED)

        aid = hub.request_adjustment(
            wid, "remove_container", "2026-09-22T07:42:00+08:00",
            detail={"container_id": "box-a2"}, reason="门店临时歇业")
        # 被驳回的不能执行
        hub.decide_adjustment(aid, False, "2026-09-22T07:43:00+08:00", reason="信息不符")
        with self.assertRaises(BusinessRuleError) as cm:
            hub.apply_adjustment(aid, "2026-09-22T07:44:00+08:00")
        self.assertEqual(cm.exception.reason, E.ADJUSTMENT_NOT_APPROVED)

        # 重新申请、批准，在封签前执行
        aid2 = hub.request_adjustment(
            wid, "remove_container", "2026-09-22T07:46:00+08:00",
            detail={"container_id": "box-a2"})
        hub.decide_adjustment(aid2, True, "2026-09-22T07:47:00+08:00",
                              approver="boss-li")
        hub.assign_driver(wid, "driver-wang", "2026-09-22T07:48:00+08:00")
        hub.load_container("scan-1", "box-a1", "2026-09-22T07:50:00+08:00")
        hub.load_container("scan-2", "box-a2", "2026-09-22T07:52:00+08:00")
        hub.apply_adjustment(aid2, "2026-09-22T08:00:00+08:00")

        # 版本升级 v1 -> v2，波次回到待领单，box-a2 摘除且留下逐件痕迹
        view = hub.wave_view(wid)
        self.assertEqual(view["delivery_version"], f"{wid}@v2")
        self.assertEqual(view["state"], "cutoff")
        self.assertEqual([c["container_id"] for c in view["containers"]], ["box-a1"])
        trace = hub.container_trace("box-a2")
        self.assertEqual(trace["state"], "unloaded")
        self.assertEqual(trace["custody"][-1]["action"], "unloaded")

        # 旧领单失效：不重新领单不能封签
        with self.assertRaises(BusinessRuleError) as cm:
            hub.seal_vehicle(wid, "driver-wang", "2026-09-22T08:05:00+08:00")
        self.assertEqual(cm.exception.reason, E.WAVE_CLOSED)

        hub.assign_driver(wid, "driver-wang", "2026-09-22T08:06:00+08:00")
        sealed = hub.seal_vehicle(wid, "driver-wang", "2026-09-22T08:20:00+08:00")
        self.assertEqual(sealed["delivery_version"], f"{wid}@v2")

        # 封签之后：即便差异单获批，业务时间晚于封签也不得变更
        aid3 = hub.request_adjustment(
            wid, "remove_container", "2026-09-22T08:30:00+08:00",
            detail={"container_id": "box-a1"})
        hub.decide_adjustment(aid3, True, "2026-09-22T08:31:00+08:00")
        with self.assertRaises(BusinessRuleError) as cm:
            hub.apply_adjustment(aid3, "2026-09-22T08:35:00+08:00")
        self.assertEqual(cm.exception.reason, E.ADJUSTMENT_TARGET_SEALED)
        self.assertIn("sealed_at", cm.exception.context)

        # 仓库回看：时间线里改单（08:00）明确早于封签（08:20），
        # 封签后的改单尝试被拒绝、根本不入时间线
        timeline = hub.wave_timeline(wid)
        applied = [e for e in timeline if e["event_type"] == "adjustment.applied"]
        seals = [e for e in timeline if e["event_type"] == "vehicle.sealed"]
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["delivery_version"], f"{wid}@v2")
        self.assertLess(applied[0]["occurred_at"], seals[0]["occurred_at"])


class OfflineUploadTest(unittest.TestCase):
    def test_offline_scans_validated_by_business_time(self) -> None:
        hub = WaveHub(":memory:")
        hub.register_vehicle(vehicle("vf", 200, bases={"F": 20}),
                             "2026-09-22T06:00:00+08:00")
        hub.register_order(order("of1", "SF1", "F", [item("sku-1", 1)]),
                           "2026-09-22T06:10:00+08:00")
        hub.register_order(order("of2", "SF2", "F", [item("sku-2", 1)]),
                           "2026-09-22T06:11:00+08:00")
        hub.register_container("box-f1", "F", "2026-09-22T06:20:00+08:00")
        hub.register_container("box-f2", "F", "2026-09-22T06:21:00+08:00")
        wid = hub.plan_waves("2026-09-22T07:00:00+08:00")["waves"][0]["wave_id"]
        hub.cutoff_wave(wid, "2026-09-22T07:30:00+08:00")
        hub.assign_driver(wid, "driver-f", "2026-09-22T07:40:00+08:00")

        # 设备离线后乱序补传一批（按业务时间排序校验）
        report = hub.upload_scans([
            {"scan_id": "sl-f2", "type": "load", "container_id": "box-f2",
             "occurred_at": "2026-09-22T08:05:00+08:00"},
            {"scan_id": "sl-f1", "type": "load", "container_id": "box-f1",
             "occurred_at": "2026-09-22T07:50:00+08:00"},
            {"scan_id": "sl-f1", "type": "load", "container_id": "box-f1",
             "occurred_at": "2026-09-22T09:30:00+08:00"},  # 同 scan_id 重传
            {"scan_id": "ss-early", "type": "sign", "container_id": "box-f1",
             "store_id": "SF1", "occurred_at": "2026-09-22T08:10:00+08:00"},  # 封签前
        ])
        statuses = [(r["scan_id"], r["status"]) for r in report]
        self.assertIn(("sl-f1", "applied"), statuses)
        self.assertIn(("sl-f2", "applied"), statuses)
        # 同 scan_id 的第二次上传：幂等，不再产生第二遍效果
        self.assertIn(("sl-f1", "duplicate"), statuses)
        early = next(r for r in report if r["scan_id"] == "ss-early")
        self.assertEqual(early["reason"], E.SCAN_OUT_OF_ORDER)

        hub.seal_vehicle(wid, "driver-f", "2026-09-22T08:20:00+08:00")

        # 恢复网络后补传：封签后才发生的装车扫描无效，签收有效
        report2 = hub.upload_scans([
            {"scan_id": "sl-again", "type": "load", "container_id": "box-f1",
             "occurred_at": "2026-09-22T09:00:00+08:00"},
            {"scan_id": "ss-f1", "type": "sign", "container_id": "box-f1",
             "store_id": "SF1", "occurred_at": "2026-09-22T10:00:00+08:00"},
        ])
        st = {r["scan_id"]: r["status"] for r in report2}
        self.assertEqual(st["sl-again"], "rejected")
        self.assertEqual(st["ss-f1"], "applied")
        self.assertEqual(report2[0]["reason"], E.SCAN_OUT_OF_ORDER)


class ReassignmentTest(unittest.TestCase):
    def _sealed_world(self) -> tuple[WaveHub, str]:
        hub = WaveHub(":memory:")
        hub.register_vehicle(
            vehicle("vg1", 500, zones=("ambient", "chilled", "frozen"),
                    bases={"G": 30}), "2026-09-22T06:00:00+08:00")
        hub.register_order(order("og1", "SG1", "G",
                                 [item("sku-f", 1, "frozen", 40)]),
                           "2026-09-22T06:10:00+08:00")
        hub.register_container("box-g1", "G", "2026-09-22T06:20:00+08:00")
        wid = hub.plan_waves("2026-09-22T07:00:00+08:00")["waves"][0]["wave_id"]
        hub.cutoff_wave(wid, "2026-09-22T07:30:00+08:00")
        hub.assign_driver(wid, "driver-g", "2026-09-22T07:40:00+08:00")
        hub.load_container("sl-g1", "box-g1", "2026-09-22T07:50:00+08:00")
        hub.seal_vehicle(wid, "driver-g", "2026-09-22T08:20:00+08:00")
        return hub, wid

    def test_reassignment_rechecks_constraints_and_keeps_custody(self) -> None:
        hub, wid = self._sealed_world()

        # 未故障不能转派
        with self.assertRaises(BusinessRuleError) as cm:
            hub.reassign_vehicle(wid, "vg2", "driver-x",
                                 "2026-09-22T09:00:00+08:00")
        self.assertEqual(cm.exception.reason, E.VEHICLE_NOT_BROKEN)

        hub.report_breakdown("vg1", "2026-09-22T09:30:00+08:00")
        view = hub.wave_view(wid)
        self.assertEqual(view["delay_reasons"][0]["reason"], E.VEHICLE_BREAKDOWN)
        pending = hub.pending_recovery()
        self.assertEqual(pending["awaiting_reassignment"][0]["vehicle_id"], "vg1")

        # 接替车温层不覆盖冷冻 → 拒绝整车转派
        hub.register_vehicle(vehicle("vg2", 500, zones=("ambient", "chilled"),
                                     bases={"G": 30}), "2026-09-22T09:35:00+08:00")
        with self.assertRaises(BusinessRuleError) as cm:
            hub.reassign_vehicle(wid, "vg2", "driver-x",
                                 "2026-09-22T09:40:00+08:00")
        self.assertEqual(cm.exception.reason, E.REASSIGNMENT_CONSTRAINT_FAILED)
        self.assertIn("ZONE_LIMIT", cm.exception.context["reasons"])

        # 合格接替车：转派成功，版本升级，容器逐件跟随
        hub.register_vehicle(
            vehicle("vg3", 600, zones=("ambient", "chilled", "frozen"),
                    bases={"G": 25}), "2026-09-22T09:41:00+08:00")
        out = hub.reassign_vehicle(wid, "vg3", "driver-z",
                                   "2026-09-22T09:45:00+08:00")
        self.assertEqual(out["delivery_version"], f"{wid}@v2")
        view = hub.wave_view(wid)
        self.assertEqual(view["state"], "transferred")
        self.assertEqual(view["vehicle_id"], "vg3")
        self.assertEqual(view["delay_reasons"][-1]["reason"], E.REASSIGNMENT)

        trace = hub.container_trace("box-g1")
        tail = [(c["action"], c.get("to_vehicle")) for c in trace["custody"][-2:]]
        self.assertEqual(tail, [("transferred", "vg3"), ("sealed", None)])
        self.assertEqual(trace["vehicle_id"], "vg3")

        # 沿新版本完成签收
        hub.sign_container("ss-g1", "box-g1", "SG1",
                           "2026-09-22T10:10:00+08:00")
        self.assertEqual(hub.wave_view(wid)["state"], "delivered")


class StoreVisibilityTest(unittest.TestCase):
    def test_store_sees_shortage_split_and_delay_reasons(self) -> None:
        hub = WaveHub(":memory:")
        hub.register_vehicle(vehicle("v1", 500, bases={"A": 30}),
                             "2026-09-22T06:00:00+08:00")
        hub.register_order(order("o1", "S1", "A",
                                 [item("sku-a", 5, weight=20)]),
                           "2026-09-22T06:10:00+08:00")
        hub.register_container("box-a1", "A", "2026-09-22T06:20:00+08:00")
        wid = hub.plan_waves("2026-09-22T07:00:00+08:00")["waves"][0]["wave_id"]
        hub.report_shortage("o1", "sku-a", expected_qty=5, actual_qty=4,
                            reason=E.SUPPLY_SHORT,
                            occurred_at="2026-09-22T07:10:00+08:00",
                            wave_id=wid, container_id="box-a1")
        hub.report_delay(wid, E.WAVE_REPLAN, "2026-09-22T07:20:00+08:00",
                         new_eta="2026-09-22T10:30:00+08:00", detail="临时加派")

        status = hub.store_status("S1")
        o = status["orders"][0]
        self.assertEqual(o["shortages"][0]["reason"], "SUPPLY_SHORT")
        self.assertEqual(o["shortages"][0]["expected_qty"] - o["shortages"][0]["actual_qty"], 1)
        self.assertEqual(o["waves"][0]["delays"][0]["reason"], "WAVE_REPLAN")
        self.assertEqual(o["waves"][0]["delays"][0]["new_eta"],
                         "2026-09-22T10:30:00+08:00")


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_seal_leaves_single_result(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / "wave.db")

        def seed() -> str:
            hub = WaveHub(db_path)
            hub.register_vehicle(vehicle("v1", 500, bases={"A": 30}),
                                 "2026-09-22T06:00:00+08:00")
            hub.register_order(order("o1", "S1", "A", [item("sku-a", 1)]),
                               "2026-09-22T06:10:00+08:00")
            hub.register_container("box-a1", "A", "2026-09-22T06:20:00+08:00")
            wid = hub.plan_waves("2026-09-22T07:00:00+08:00")["waves"][0]["wave_id"]
            hub.cutoff_wave(wid, "2026-09-22T07:30:00+08:00")
            hub.assign_driver(wid, "driver-z", "2026-09-22T07:40:00+08:00")
            hub.load_container("sl-1", "box-a1", "2026-09-22T07:50:00+08:00")
            return wid

        wid = seed()
        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def seal() -> None:
            hub = WaveHub(db_path)  # 独立连接与投影，共享同一事件库
            barrier.wait()
            try:
                hub.seal_vehicle(wid, "driver-z", "2026-09-22T08:20:00+08:00")
                outcomes.append("sealed")
            except BusinessRuleError as exc:
                outcomes.append(exc.reason)

        threads = [threading.Thread(target=seal) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(outcomes), sorted(["sealed", E.CONCURRENT_SEAL]))
        # 事件库里只有一条封签事件
        final = WaveHub(db_path)
        seals = [e for e in final.store.all_events()
                 if e["event_type"] == "vehicle.sealed"]
        self.assertEqual(len(seals), 1)
        self.assertEqual(final.wave_view(wid)["state"], "sealed")


class RecoveryTest(unittest.TestCase):
    def test_restart_restores_inflight_work(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / "wave.db")

        hub = WaveHub(db_path)
        hub.register_vehicle(vehicle("vg1", 500, bases={"G": 30}),
                             "2026-09-22T06:00:00+08:00")
        hub.register_vehicle(vehicle("vg2", 600, bases={"G": 30}),
                             "2026-09-22T06:01:00+08:00")
        hub.register_order(order("og1", "SG1", "G", [item("sku-1", 1, weight=20)]),
                           "2026-09-22T06:10:00+08:00")
        hub.register_order(order("og2", "SG2", "G", [item("sku-2", 1, weight=20)]),
                           "2026-09-22T06:11:00+08:00")
        hub.register_container("box-g1", "G", "2026-09-22T06:20:00+08:00")
        hub.register_container("box-g2", "G", "2026-09-22T06:21:00+08:00")
        wid = hub.plan_waves("2026-09-22T07:00:00+08:00")["waves"][0]["wave_id"]
        hub.cutoff_wave(wid, "2026-09-22T07:30:00+08:00")
        hub.assign_driver(wid, "driver-g", "2026-09-22T07:40:00+08:00")
        hub.load_container("sl-1", "box-g1", "2026-09-22T07:50:00+08:00")
        hub.load_container("sl-2", "box-g2", "2026-09-22T07:52:00+08:00")
        hub.seal_vehicle(wid, "driver-g", "2026-09-22T08:20:00+08:00")
        hub.report_breakdown("vg1", "2026-09-22T09:00:00+08:00")
        hub.sign_container("ss-1", "box-g1", "SG1", "2026-09-22T09:20:00+08:00")
        hub.store.close()

        # —— 进程恢复 ——
        hub2 = WaveHub(db_path)
        pending = hub2.pending_recovery()
        self.assertEqual(pending["awaiting_reassignment"][0]["vehicle_id"], "vg1")
        unreturned = {c["container_id"]: c["state"]
                      for c in pending["unreturned_containers"]}
        self.assertEqual(unreturned["box-g1"], "signed")   # 已签收未回收
        self.assertEqual(unreturned["box-g2"], "sealed")   # 仍在车上
        self.assertEqual(pending["waves"][0]["state"], "sealed")

        # 恢复后继续推进：转派 → 第二箱签收 → 全部回收
        hub2.reassign_vehicle(wid, "vg2", "driver-g2",
                              "2026-09-22T09:40:00+08:00")
        hub2.sign_container("ss-2", "box-g2", "SG2",
                            "2026-09-22T10:00:00+08:00")
        hub2.return_container("sr-1", "box-g1", "2026-09-22T10:30:00+08:00")
        hub2.return_container("sr-2", "box-g2", "2026-09-22T10:31:00+08:00")
        self.assertEqual(hub2.wave_view(wid)["state"], "returned")


class TimelineTest(unittest.TestCase):
    def test_timeline_settles_adjustment_vs_seal_order(self) -> None:
        hub = WaveHub(":memory:")
        hub.register_vehicle(vehicle("v1", 500, bases={"A": 30}),
                             "2026-09-22T06:00:00+08:00")
        hub.register_order(order("o1", "S1", "A", [item("sku-a", 1, weight=20)]),
                           "2026-09-22T06:10:00+08:00")
        hub.register_container("box-a1", "A", "2026-09-22T06:20:00+08:00")
        wid = hub.plan_waves("2026-09-22T07:00:00+08:00")["waves"][0]["wave_id"]
        hub.cutoff_wave(wid, "2026-09-22T07:30:00+08:00")
        hub.assign_driver(wid, "driver-w", "2026-09-22T07:40:00+08:00")
        hub.load_container("sl-1", "box-a1", "2026-09-22T07:50:00+08:00")
        hub.seal_vehicle(wid, "driver-w", "2026-09-22T08:20:00+08:00")

        timeline = hub.wave_timeline(wid)
        types = [e["event_type"] for e in timeline]
        self.assertEqual(
            types.index("container.loaded") < types.index("vehicle.sealed"), True,
        )
        seal_time = next(e["occurred_at"] for e in timeline if e["event_type"] == "vehicle.sealed")
        self.assertEqual(seal_time, "2026-09-22T08:20:00+08:00")
        # 记录时间（上传时间）与业务时间分列，离线补传也能还原真实先后
        self.assertTrue(all(e["recorded_at"] for e in timeline))


if __name__ == "__main__":
    unittest.main()
