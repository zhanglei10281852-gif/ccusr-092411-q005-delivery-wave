"""截单语义与配送版本：截单前合并、截单后差异单、封签后拒绝变更。"""
from __future__ import annotations

import unittest

import bootstrap
from wavehub import CutoffError, StateError


class CutoffAndVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = bootstrap.new_hub()
        self.hub.create_wave("W1", cutoff_at=bootstrap.CUTOFF)
        bootstrap.add_store(self.hub, "S1", "W1")

    def tearDown(self) -> None:
        self.hub.close()

    def test_orders_merge_before_cutoff(self):
        hub = self.hub
        first = hub.receive_order("O1", "S1", "W1", [{"product_id": "apple", "qty": 2}],
                                  created_at="2026-09-23T09:00:00+08:00")
        second = hub.receive_order(
            "O2", "S1", "W1",
            [{"product_id": "apple", "qty": 3}, {"product_id": "soap", "qty": 1}],
            created_at="2026-09-23T10:00:00+08:00")
        self.assertEqual(first, "O1")
        self.assertEqual(second, "O1")  # 合并进既有订单
        merged = hub.conn.execute(
            "select status, merged_into from store_order where order_id = 'O2'"
        ).fetchone()
        self.assertEqual(merged["status"], "merged")
        self.assertEqual(merged["merged_into"], "O1")
        view = hub.store_view("S1", "W1")
        lines = {line["product_id"]: line["qty_ordered"] for line in view["lines"]}
        self.assertEqual(lines, {"apple": 5, "soap": 1})

    def test_receive_after_cutoff_rejected(self):
        hub = self.hub
        hub.receive_order("O1", "S1", "W1", [{"product_id": "apple", "qty": 1}],
                          created_at="2026-09-23T09:00:00+08:00")
        hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
        with self.assertRaises(CutoffError):
            hub.receive_order("O2", "S1", "W1", [{"product_id": "apple", "qty": 1}],
                              created_at="2026-09-23T19:00:00+08:00")

    def _planned_route(self) -> str:
        hub = self.hub
        hub.add_vehicle("V1", "沪A1", capacity=1000, temp_zones=["ambient"])
        for i in range(1, 4):
            hub.add_container(f"C{i}")
        hub.receive_order("O1", "S1", "W1", [{"product_id": "apple", "qty": 2}],
                          created_at="2026-09-23T09:00:00+08:00")
        hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
        result = hub.plan_wave("W1", depart_at=bootstrap.ts("08:00"),
                              max_route_minutes=600)
        return result["routes"][0]["route_id"]

    def test_approved_difference_creates_new_version(self):
        hub = self.hub
        route_id = self._planned_route()
        manifest = hub.get_manifest(route_id)
        self.assertEqual(manifest["version_no"], 1)
        self.assertEqual(manifest["stops"][0]["lines"][0]["qty"], 2)
        diff_id = hub.submit_difference(
            route_id,
            [{"change_type": "adjust", "store_id": "S1",
              "product_id": "apple", "qty": 5}],
            reason="门店临时加单", created_at=bootstrap.ts("07:00"))
        # 批准前清单不变
        self.assertEqual(hub.get_manifest(route_id)["stops"][0]["lines"][0]["qty"], 2)
        new_version = hub.approve_difference(diff_id, decided_by="调度甲",
                                             at=bootstrap.ts("07:05"))
        self.assertEqual(new_version, 2)
        manifest = hub.pull_manifest(route_id, pulled_by="司机乙",
                                     at=bootstrap.ts("07:10"))
        self.assertEqual(manifest["version_no"], 2)
        self.assertEqual(manifest["stops"][0]["lines"][0]["qty"], 5)
        # 历史版本仍可回看
        self.assertEqual(hub.get_manifest(route_id, 1)["stops"][0]["lines"][0]["qty"], 2)
        history = hub.route_history(route_id)
        self.assertEqual([v["status"] for v in history["versions"]],
                         ["superseded", "published"])
        self.assertEqual(history["versions"][1]["cause"], f"difference:{diff_id}")

    def test_difference_cannot_touch_sealed_route(self):
        hub = self.hub
        route_id = self._planned_route()
        hub.record_scan("SC1", "C1", "load", route_id, version_no=1,
                        occurred_at=bootstrap.ts("07:30"))
        hub.seal_route(route_id, version_no=1, seal_no="SEAL-1",
                       operator="仓管丙", at=bootstrap.ts("07:50"))
        diff_id = hub.submit_difference(
            route_id,
            [{"change_type": "adjust", "store_id": "S1",
              "product_id": "apple", "qty": 9}],
            reason="封签后门店要求改单", created_at=bootstrap.ts("08:00"))
        with self.assertRaises(StateError) as ctx:
            hub.approve_difference(diff_id, decided_by="调度甲",
                                   at=bootstrap.ts("08:05"))
        self.assertIn("封签", str(ctx.exception))
        # 回看记录：改单发生在封签之后，一目了然
        history = hub.route_history(route_id)
        self.assertEqual(history["seals"][0]["sealed_at"], bootstrap.ts("07:50"))
        self.assertGreater(history["differences"][0]["created_at"],
                           history["seals"][0]["sealed_at"])
        self.assertEqual(history["differences"][0]["status"], "pending")
        self.assertEqual(history["current_version"], 1)

    def test_difference_rejected_keeps_version(self):
        hub = self.hub
        route_id = self._planned_route()
        diff_id = hub.submit_difference(
            route_id,
            [{"change_type": "adjust", "store_id": "S1",
              "product_id": "apple", "qty": 7}],
            reason="门店要求加量", created_at=bootstrap.ts("07:00"))
        hub.reject_difference(diff_id, decided_by="调度甲", at=bootstrap.ts("07:05"))
        self.assertEqual(hub.get_manifest(route_id)["stops"][0]["lines"][0]["qty"], 2)
        history = hub.route_history(route_id)
        self.assertEqual(history["differences"][0]["status"], "rejected")
        self.assertEqual(len(history["versions"]), 1)

    def test_scan_version_must_match_business_time(self):
        hub = self.hub
        route_id = self._planned_route()
        diff_id = hub.submit_difference(
            route_id,
            [{"change_type": "adjust", "store_id": "S1",
              "product_id": "apple", "qty": 4}],
            reason="门店改量", created_at=bootstrap.ts("07:00"))
        hub.approve_difference(diff_id, decided_by="调度甲", at=bootstrap.ts("07:05"))
        stale = hub.record_scan("SC1", "C1", "load", route_id, version_no=1,
                                occurred_at=bootstrap.ts("07:30"))
        self.assertEqual(stale["result"], "rejected")
        self.assertIn("版本", stale["reason"])
        fresh = hub.record_scan("SC2", "C1", "load", route_id, version_no=2,
                                occurred_at=bootstrap.ts("07:31"))
        self.assertEqual(fresh["result"], "accepted")


if __name__ == "__main__":
    unittest.main()
