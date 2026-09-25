"""恢复运行：进程重启后，待转派车辆与未回收容器仍处在正确环节。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import bootstrap
from wavehub import WaveHub


def build_state(path: str) -> None:
    hub = WaveHub(path)
    hub.add_product("apple", "苹果", temp_zone="ambient",
                    coload_group="general", unit_weight=10)
    hub.add_product("fish", "冻鱼", temp_zone="frozen",
                    coload_group="seafood", unit_weight=20)
    hub.create_wave("W1", cutoff_at=bootstrap.CUTOFF)
    hub.add_store("S1", "门店S1")
    hub.set_store_window("S1", "W1", open_at=bootstrap.ts("09:00"),
                         close_at=bootstrap.ts("12:00"))
    hub.add_store("S2", "门店S2")
    hub.set_store_window("S2", "W1", open_at=bootstrap.ts("09:00"),
                         close_at=bootstrap.ts("12:00"))
    hub.add_vehicle("V1", "沪A1", capacity=1000, temp_zones=["ambient"])
    hub.add_vehicle("V2", "沪A2", capacity=1000, temp_zones=["frozen"])
    hub.add_vehicle("V3", "沪A3", capacity=1000, temp_zones=["ambient"])
    for i in range(1, 4):
        hub.add_container(f"C{i}")
    hub.receive_order("O1", "S1", "W1", [{"product_id": "apple", "qty": 2}],
                      created_at="2026-09-23T09:00:00+08:00")
    hub.receive_order("O2", "S2", "W1", [{"product_id": "fish", "qty": 30}],
                      created_at="2026-09-23T09:05:00+08:00")
    hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
    hub.plan_wave("W1", depart_at=bootstrap.ts("08:00"), max_route_minutes=600)
    # R1（V1，S1 苹果）：装车封签后车辆故障，进入待转派
    hub.record_scan("SC-1", "C1", "load", "W1-R1", version_no=1,
                    occurred_at=bootstrap.ts("07:30"))
    hub.seal_route("W1-R1", version_no=1, seal_no="SEAL-1",
                   operator="仓管丙", at=bootstrap.ts("07:50"))
    hub.report_breakdown("V1", at=bootstrap.ts("07:55"), reason="无法启动")
    # R2（V2，S2 冻鱼 600kg → C2、C3）：发车后 C2 已签收，C3 仍在车上
    hub.record_scan("SC-2", "C2", "load", "W1-R2", version_no=1,
                    occurred_at=bootstrap.ts("07:32"))
    hub.record_scan("SC-3", "C3", "load", "W1-R2", version_no=1,
                    occurred_at=bootstrap.ts("07:33"))
    hub.seal_route("W1-R2", version_no=1, seal_no="SEAL-2",
                   operator="仓管丙", at=bootstrap.ts("07:52"))
    hub.depart_route("W1-R2", at=bootstrap.ts("08:00"))
    hub.record_scan("SC-4", "C2", "handover", "W1-R2", version_no=1,
                    store_id="S2", occurred_at=bootstrap.ts("09:30"))
    hub.close()


class RecoveryTest(unittest.TestCase):
    def test_pending_transfer_and_unrecovered_containers_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "hub.db")
            build_state(path)
            # 模拟系统恢复：重新打开同一数据库
            hub = WaveHub(path)
            report = hub.recovery_report()
            self.assertEqual(report["broken_vehicles"], ["V1"])
            self.assertEqual(len(report["pending_transfers"]), 1)
            pending = report["pending_transfers"][0]
            self.assertEqual(pending["route_id"], "W1-R1")
            self.assertEqual(pending["vehicle_id"], "V1")
            self.assertEqual(pending["vehicle_status"], "broken_down")
            self.assertEqual(pending["loaded_containers"], 1)
            locations = {c["container_id"]: c
                         for c in report["unrecovered_containers"]}
            self.assertEqual(locations["C1"]["status"], "loaded")
            self.assertEqual(locations["C1"]["route_id"], "W1-R1")
            self.assertEqual(locations["C2"]["status"], "handed_over")
            self.assertEqual(locations["C2"]["store_id"], "S2")
            self.assertEqual(locations["C3"]["status"], "loaded")
            self.assertEqual(locations["C3"]["route_id"], "W1-R2")
            # 作废封签与线路历史在恢复后依然可查
            history = hub.route_history("W1-R1")
            self.assertEqual(history["status"], "pending_transfer")
            self.assertEqual(history["seals"][0]["void_reason"], "无法启动")
            # 恢复运行后业务可以继续：R1 转派到 V3 并重新封签发车
            hub.transfer_route("W1-R1", "V3", at=bootstrap.ts("10:00"))
            hub.seal_route("W1-R1", version_no=1, seal_no="SEAL-3",
                           operator="仓管丙", at=bootstrap.ts("10:10"))
            hub.depart_route("W1-R1", at=bootstrap.ts("10:20"))
            result = hub.record_scan("SC-5", "C1", "handover", "W1-R1",
                                     version_no=1, store_id="S1",
                                     occurred_at=bootstrap.ts("11:00"))
            self.assertEqual(result["result"], "accepted")
            hub.close()


if __name__ == "__main__":
    unittest.main()
