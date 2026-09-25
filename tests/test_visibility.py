"""门店查询：缺货、拆单、延迟都要能看到具体原因。"""
from __future__ import annotations

import unittest

import bootstrap


class VisibilityTest(unittest.TestCase):
    def tearDown(self) -> None:
        self.hub.close()

    def test_shortage_reason_visible_to_store(self):
        hub = bootstrap.planned_hub()
        self.hub = hub
        hub.record_shortage("W1-R1", "S1", "apple", qty=1,
                            reason="仓库库存不足，明日补送", at=bootstrap.ts("07:40"))
        view = hub.store_view("S1", "W1")
        line = next(l for l in view["lines"] if l["product_id"] == "apple")
        self.assertEqual(line["qty_ordered"], 2)
        self.assertEqual(line["qty_short"], 1)
        shortage = next(e for e in view["exceptions"] if e["type"] == "out_of_stock")
        self.assertEqual(shortage["detail"], "仓库库存不足，明日补送")
        self.assertEqual(shortage["product_id"], "apple")

    def test_split_reason_and_other_route_visible(self):
        hub = bootstrap.new_hub()
        self.hub = hub
        hub.create_wave("W1", cutoff_at=bootstrap.CUTOFF)
        bootstrap.add_store(hub, "S1", "W1")
        hub.add_vehicle("V1", "沪A1", capacity=100, temp_zones=["ambient"])
        hub.add_vehicle("V2", "沪A2", capacity=100, temp_zones=["ambient"])
        for i in range(4):
            hub.add_container(f"C{i}")
        hub.receive_order("O1", "S1", "W1",
                          [{"product_id": "apple", "qty": 8},
                           {"product_id": "soap", "qty": 6}],
                          created_at="2026-09-23T09:00:00+08:00")
        hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
        hub.plan_wave("W1", depart_at=bootstrap.ts("08:00"), max_route_minutes=600)
        view = hub.store_view("S1", "W1")
        split = next(e for e in view["exceptions"] if e["type"] == "split")
        self.assertIn("载重", split["detail"])
        apple = next(l for l in view["lines"] if l["product_id"] == "apple")
        soap = next(l for l in view["lines"] if l["product_id"] == "soap")
        self.assertNotEqual(apple["placements"][0]["route_id"],
                            soap["placements"][0]["route_id"])
        self.assertEqual(len(view["stops"]), 2)

    def test_delay_reason_and_new_eta_visible(self):
        hub = bootstrap.planned_hub()
        self.hub = hub
        hub.report_delay("W1-R1", minutes=45, reason="高速封路绕行",
                         at=bootstrap.ts("08:30"))
        view = hub.store_view("S1", "W1")
        stop = next(s for s in view["stops"] if s["route_id"] == "W1-R1")
        self.assertEqual(stop["delay_reason"], "高速封路绕行")
        self.assertEqual(stop["eta"], bootstrap.ts("09:00"))
        self.assertEqual(stop["eta_delayed"], bootstrap.ts("09:45"))
        delayed = next(e for e in view["exceptions"] if e["type"] == "delayed")
        self.assertIn("高速封路绕行", delayed["detail"])

    def test_store_sees_sign_progress(self):
        hub = bootstrap.planned_hub()
        self.hub = hub
        hub.record_scan("SC-L", "C1", "load", "W1-R1", version_no=1,
                        occurred_at=bootstrap.ts("07:30"))
        hub.seal_route("W1-R1", version_no=1, seal_no="SEAL-1",
                       operator="仓管丙", at=bootstrap.ts("07:50"))
        hub.depart_route("W1-R1", at=bootstrap.ts("08:00"))
        hub.record_scan("SC-H", "C1", "handover", "W1-R1", version_no=1,
                        store_id="S1", occurred_at=bootstrap.ts("09:30"))
        view = hub.store_view("S1", "W1")
        stop = view["stops"][0]
        self.assertTrue(stop["signed"])
        self.assertEqual(stop["seal_no"], "SEAL-1")
        self.assertEqual(stop["containers_handed_over"], 1)


if __name__ == "__main__":
    unittest.main()
