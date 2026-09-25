"""波次编排：收货窗口、载重与温层、共载限制、线路时长、箱筐回收。"""
from __future__ import annotations

import unittest

import bootstrap
from wavehub import InfeasiblePlanError


class PlannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = bootstrap.new_hub()
        self.hub.create_wave("W1", cutoff_at=bootstrap.CUTOFF)

    def tearDown(self) -> None:
        self.hub.close()

    def plan(self, **kwargs):
        return self.hub.plan_wave("W1", depart_at=bootstrap.ts("08:00"), **kwargs)

    def order(self, order_id, store_id, lines):
        self.hub.receive_order(order_id, store_id, "W1", lines,
                               created_at="2026-09-23T09:00:00+08:00")

    def test_capacity_forces_split_and_records_reason(self):
        hub = self.hub
        bootstrap.add_store(hub, "S1", "W1")
        hub.add_vehicle("V1", "沪A1", capacity=100, temp_zones=["ambient"])
        hub.add_vehicle("V2", "沪A2", capacity=100, temp_zones=["ambient"])
        for i in range(4):
            hub.add_container(f"C{i}")
        self.order("O1", "S1", [{"product_id": "apple", "qty": 8},
                                {"product_id": "soap", "qty": 6}])
        hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
        result = self.plan(max_route_minutes=600)
        self.assertEqual(len(result["routes"]), 2)
        self.assertEqual(len(result["splits"]), 1)
        self.assertEqual(result["splits"][0]["order_id"], "O1")
        self.assertIn("载重", result["splits"][0]["reason"])

    def test_temperature_zone_selects_vehicle(self):
        hub = self.hub
        bootstrap.add_store(hub, "S1", "W1")
        hub.add_vehicle("V1", "沪A1", capacity=1000, temp_zones=["ambient"])
        hub.add_container("C1")
        self.order("O1", "S1", [{"product_id": "fish", "qty": 2}])
        hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
        with self.assertRaises(InfeasiblePlanError):
            self.plan(max_route_minutes=600)
        hub.add_vehicle("V3", "沪A3", capacity=500, temp_zones=["frozen"])
        result = self.plan(max_route_minutes=600)
        self.assertEqual(result["routes"][0]["vehicle_id"], "V3")

    def test_coload_restriction_separates_groups(self):
        hub = self.hub
        bootstrap.add_store(hub, "S1", "W1")
        hub.add_vehicle("V1", "沪A1", capacity=1000, temp_zones=["ambient"])
        hub.add_vehicle("V2", "沪A2", capacity=1000,
                        temp_zones=["ambient", "chilled"])
        for i in range(4):
            hub.add_container(f"C{i}")
        self.order("O1", "S1", [{"product_id": "clam", "qty": 2},
                                {"product_id": "soap", "qty": 2}])
        hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
        result = self.plan(max_route_minutes=600)
        self.assertEqual(len(result["routes"]), 2)
        self.assertIn("共载", result["splits"][0]["reason"])
        vehicles = sorted(r["vehicle_id"] for r in result["routes"])
        self.assertEqual(vehicles, ["V1", "V2"])

    def test_stops_follow_receiving_windows(self):
        hub = self.hub
        bootstrap.add_store(hub, "S1", "W1", window=("10:00", "11:00"))
        bootstrap.add_store(hub, "S2", "W1", window=("08:30", "09:00"))
        hub.add_vehicle("V1", "沪A1", capacity=1000, temp_zones=["ambient"])
        for i in range(4):
            hub.add_container(f"C{i}")
        self.order("O1", "S1", [{"product_id": "apple", "qty": 2}])
        self.order("O2", "S2", [{"product_id": "apple", "qty": 2}])
        hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
        result = self.plan(max_route_minutes=600)
        self.assertEqual(len(result["routes"]), 1)
        stops = result["routes"][0]["stops"]
        self.assertEqual([s["store_id"] for s in stops], ["S2", "S1"])
        self.assertEqual(stops[0]["eta"], bootstrap.ts("08:30"))
        self.assertEqual(stops[1]["eta"], bootstrap.ts("10:00"))

    def test_missed_window_is_infeasible(self):
        hub = self.hub
        bootstrap.add_store(hub, "S1", "W1", window=("08:00", "08:20"))
        hub.add_vehicle("V1", "沪A1", capacity=1000, temp_zones=["ambient"])
        hub.add_container("C1")
        self.order("O1", "S1", [{"product_id": "apple", "qty": 1}])
        hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
        with self.assertRaises(InfeasiblePlanError) as ctx:
            self.plan(max_route_minutes=600)  # 缺省行驶 30 分钟，08:30 才到
        self.assertIn("窗口", str(ctx.exception))

    def test_route_duration_limit(self):
        hub = self.hub
        bootstrap.add_store(hub, "S1", "W1", window=("08:00", "20:00"))
        bootstrap.add_store(hub, "S2", "W1", window=("08:00", "20:00"))
        hub.add_vehicle("V1", "沪A1", capacity=1000, temp_zones=["ambient"])
        hub.add_vehicle("V2", "沪A2", capacity=1000, temp_zones=["ambient"])
        for i in range(4):
            hub.add_container(f"C{i}")
        self.order("O1", "S1", [{"product_id": "apple", "qty": 2}])
        self.order("O2", "S2", [{"product_id": "apple", "qty": 2}])
        hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
        travel = lambda a, b: 60.0  # noqa: E731
        result = self.plan(max_route_minutes=150, travel=travel)
        self.assertEqual(len(result["routes"]), 2)  # 同车 200 分钟超限，必须拆分
        hub2 = bootstrap.new_hub()
        hub2.create_wave("W1", cutoff_at=bootstrap.CUTOFF)
        bootstrap.add_store(hub2, "S1", "W1", window=("08:00", "20:00"))
        bootstrap.add_store(hub2, "S2", "W1", window=("08:00", "20:00"))
        hub2.add_vehicle("V1", "沪A1", capacity=1000, temp_zones=["ambient"])
        hub2.add_container("C1")
        hub2.add_container("C2")
        hub2.receive_order("O1", "S1", "W1", [{"product_id": "apple", "qty": 2}],
                           created_at="2026-09-23T09:00:00+08:00")
        hub2.receive_order("O2", "S2", "W1", [{"product_id": "apple", "qty": 2}],
                           created_at="2026-09-23T09:00:00+08:00")
        hub2.cutoff_wave("W1", at=bootstrap.CUTOFF)
        with self.assertRaises(InfeasiblePlanError):
            hub2.plan_wave("W1", depart_at=bootstrap.ts("08:00"),
                          max_route_minutes=150, travel=travel)
        hub2.close()

    def test_recovery_empties_count_against_capacity(self):
        hub = self.hub
        bootstrap.add_store(hub, "S1", "W1")
        hub.add_vehicle("V1", "沪A1", capacity=100, temp_zones=["ambient"])
        hub.add_container("C1")
        hub.add_recovery_task("S1", "W1", 5)  # 5 × 25 = 125 > 100
        self.order("O1", "S1", [{"product_id": "apple", "qty": 8}])
        hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
        with self.assertRaises(InfeasiblePlanError) as ctx:
            self.plan(max_route_minutes=600)
        self.assertIn("回收", str(ctx.exception))
        hub.add_vehicle("V9", "沪A9", capacity=200, temp_zones=["ambient"])
        result = self.plan(max_route_minutes=600)
        self.assertEqual(result["routes"][0]["vehicle_id"], "V9")

    def test_recovery_only_store_gets_a_stop(self):
        hub = self.hub
        bootstrap.add_store(hub, "S1", "W1")
        bootstrap.add_store(hub, "S2", "W1")
        hub.add_vehicle("V1", "沪A1", capacity=1000, temp_zones=["ambient"])
        hub.add_container("C1")
        hub.add_recovery_task("S2", "W1", 3)
        self.order("O1", "S1", [{"product_id": "apple", "qty": 1}])
        hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
        result = self.plan(max_route_minutes=600)
        self.assertEqual(len(result["routes"]), 1)
        stops = {s["store_id"]: s for s in result["routes"][0]["stops"]}
        self.assertIn("S2", stops)
        self.assertEqual(stops["S2"]["containers"], [])

    def test_plan_is_deterministic(self):
        def build():
            hub = bootstrap.new_hub()
            hub.create_wave("W1", cutoff_at=bootstrap.CUTOFF)
            bootstrap.add_store(hub, "S1", "W1")
            bootstrap.add_store(hub, "S2", "W1")
            hub.add_vehicle("V1", "沪A1", capacity=1000, temp_zones=["ambient"])
            hub.add_vehicle("V2", "沪A2", capacity=1000, temp_zones=["frozen"])
            for i in range(4):
                hub.add_container(f"C{i}")
            hub.receive_order("O1", "S1", "W1", [{"product_id": "apple", "qty": 2}],
                              created_at="2026-09-23T09:00:00+08:00")
            hub.receive_order("O2", "S2", "W1", [{"product_id": "fish", "qty": 2}],
                              created_at="2026-09-23T09:00:00+08:00")
            hub.cutoff_wave("W1", at=bootstrap.CUTOFF)
            result = hub.plan_wave("W1", depart_at=bootstrap.ts("08:00"),
                                  max_route_minutes=600)
            hub.close()
            return result

        self.assertEqual(build(), build())


if __name__ == "__main__":
    unittest.main()
