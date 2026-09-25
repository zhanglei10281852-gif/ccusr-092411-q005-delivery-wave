"""车辆故障与整车转派：车上容器整体换车，已交接容器仍逐件追踪。"""
from __future__ import annotations

import unittest

import bootstrap
from wavehub import StateError, TransferError


class TransferTest(unittest.TestCase):
    def setUp(self) -> None:
        hub = bootstrap.new_hub()
        hub.create_wave("W1", cutoff_at=bootstrap.CUTOFF)
        bootstrap.add_store(hub, "S1", "W1")
        bootstrap.add_store(hub, "S2", "W1")
        hub.add_vehicle("V1", "沪A1", capacity=1000,
                        temp_zones=["ambient", "frozen"])
        hub.add_vehicle("V3", "沪A3", capacity=1000,
                        temp_zones=["ambient", "frozen"])
        hub.add_vehicle("V4", "沪A4", capacity=1000, temp_zones=["ambient"])
        hub.add_vehicle("V5", "沪A5", capacity=30, temp_zones=["frozen"])
        for i in range(1, 4):
            hub.add_container(f"C{i}")
        hub.receive_order("O1", "S1", "W1", [{"product_id": "apple", "qty": 2}],
                          created_at="2026-09-23T09:00:00+08:00")
        hub.receive_order("O2", "S2", "W1", [{"product_id": "fish", "qty": 2}],
                          created_at="2026-09-23T09:05:00+08:00")
        hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
        result = hub.plan_wave("W1", depart_at=bootstrap.ts("08:00"),
                              max_route_minutes=600)
        self.assertEqual(len(result["routes"]), 1)  # V1 双温层，单线路两停靠
        self.hub = hub
        self.route_id = result["routes"][0]["route_id"]

    def tearDown(self) -> None:
        self.hub.close()

    def _load_all_and_seal(self):
        self.hub.record_scan("SC-1", "C1", "load", self.route_id, version_no=1,
                             occurred_at=bootstrap.ts("07:30"))
        self.hub.record_scan("SC-2", "C2", "load", self.route_id, version_no=1,
                             occurred_at=bootstrap.ts("07:31"))
        self.hub.seal_route(self.route_id, version_no=1, seal_no="SEAL-1",
                            operator="仓管丙", at=bootstrap.ts("07:50"))

    def test_breakdown_then_whole_vehicle_transfer(self):
        hub = self.hub
        self._load_all_and_seal()
        affected = hub.report_breakdown("V1", at=bootstrap.ts("08:30"),
                                        reason="发动机故障")
        self.assertEqual(affected, [self.route_id])
        route = hub.conn.execute("select * from route where route_id = ?",
                                 (self.route_id,)).fetchone()
        self.assertEqual(route["status"], "pending_transfer")
        # 原封签作废留痕
        history = hub.route_history(self.route_id)
        self.assertEqual(history["seals"][0]["void_reason"], "发动机故障")
        # 整车转派：车上容器随线路换车，不需要重新扫描
        hub.transfer_route(self.route_id, "V3", at=bootstrap.ts("09:00"))
        route = hub.conn.execute("select * from route where route_id = ?",
                                 (self.route_id,)).fetchone()
        self.assertEqual(route["vehicle_id"], "V3")
        self.assertEqual(route["status"], "loading")
        containers = hub.conn.execute(
            "select container_id, status from container order by container_id"
        ).fetchall()
        self.assertEqual([(c["container_id"], c["status"]) for c in containers],
                         [("C1", "loaded"), ("C2", "loaded"), ("C3", "at_warehouse")])
        # 换车后重新封签发车，交接继续沿同一配送版本推进
        hub.seal_route(self.route_id, version_no=1, seal_no="SEAL-2",
                       operator="仓管丙", at=bootstrap.ts("09:10"))
        hub.depart_route(self.route_id, at=bootstrap.ts("09:20"))
        result = hub.record_scan("SC-H", "C1", "handover", self.route_id,
                                 version_no=1, store_id="S1",
                                 occurred_at=bootstrap.ts("10:00"))
        self.assertEqual(result["result"], "accepted")

    def test_handed_over_containers_stay_tracked_at_store(self):
        hub = self.hub
        self._load_all_and_seal()
        hub.depart_route(self.route_id, at=bootstrap.ts("08:00"))
        hub.record_scan("SC-H", "C1", "handover", self.route_id, version_no=1,
                        store_id="S1", occurred_at=bootstrap.ts("09:30"))
        hub.report_breakdown("V1", at=bootstrap.ts("09:45"), reason="变速箱故障")
        hub.transfer_route(self.route_id, "V3", at=bootstrap.ts("10:00"))
        # 已交接的 C1 不受转派影响，仍在门店环节逐件可查
        c1 = hub.conn.execute("select * from container where container_id = 'C1'"
                              ).fetchone()
        self.assertEqual(c1["status"], "handed_over")
        self.assertEqual(c1["current_store_id"], "S1")
        report = hub.recovery_report()
        locations = {c["container_id"]: c for c in report["unrecovered_containers"]}
        self.assertEqual(locations["C1"]["store_id"], "S1")
        self.assertEqual(locations["C2"]["route_id"], self.route_id)
        # 门店的空箱之后仍可按件回收
        result = hub.record_scan("SC-R", "C1", "recover", self.route_id,
                                 version_no=1, store_id="S1",
                                 occurred_at=bootstrap.ts("11:00"))
        self.assertEqual(result["result"], "accepted")

    def test_transfer_checks_capacity_and_temperature(self):
        hub = self.hub
        self._load_all_and_seal()
        hub.depart_route(self.route_id, at=bootstrap.ts("08:00"))
        hub.record_scan("SC-H", "C1", "handover", self.route_id, version_no=1,
                        store_id="S1", occurred_at=bootstrap.ts("09:30"))
        hub.report_breakdown("V1", at=bootstrap.ts("09:45"))
        # 剩余未签收的是冷冻鱼：常温车温层不符，小冷冻车载重不足
        with self.assertRaises(TransferError) as zone_err:
            hub.transfer_route(self.route_id, "V4", at=bootstrap.ts("10:00"))
        self.assertIn("温层", str(zone_err.exception))
        with self.assertRaises(TransferError) as cap_err:
            hub.transfer_route(self.route_id, "V5", at=bootstrap.ts("10:00"))
        self.assertIn("载重", str(cap_err.exception))
        hub.transfer_route(self.route_id, "V3", at=bootstrap.ts("10:05"))

    def test_transfer_requires_pending_state(self):
        with self.assertRaises(StateError):
            self.hub.transfer_route(self.route_id, "V3", at=bootstrap.ts("09:00"))

    def test_transfer_counts_not_yet_loaded_goods(self):
        hub = self.hub
        hub.add_vehicle("V6", "沪A6", capacity=50,
                        temp_zones=["ambient", "frozen"])
        hub.record_scan("SC-1", "C1", "load", self.route_id, version_no=1,
                        occurred_at=bootstrap.ts("07:30"))
        # 装到一半车辆故障：C2 还在仓库，剩余载重必须把它算进去
        hub.report_breakdown("V1", at=bootstrap.ts("07:35"), reason="无法启动")
        with self.assertRaises(TransferError) as ctx:
            hub.transfer_route(self.route_id, "V6", at=bootstrap.ts("08:00"))
        self.assertIn("载重", str(ctx.exception))
        hub.transfer_route(self.route_id, "V3", at=bootstrap.ts("08:05"))
        # 转派后未装车的容器在新车上继续装
        result = hub.record_scan("SC-2", "C2", "load", self.route_id, version_no=1,
                                 occurred_at=bootstrap.ts("08:10"))
        self.assertEqual(result["result"], "accepted")


if __name__ == "__main__":
    unittest.main()
