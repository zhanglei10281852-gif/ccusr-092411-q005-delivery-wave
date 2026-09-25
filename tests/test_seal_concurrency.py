"""并发封签：无论线程内竞争还是跨连接竞争，都只保留一个封签结果。"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

import bootstrap
from wavehub import SealConflictError, StateError, VersionMismatchError, WaveHub


class SealConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = bootstrap.planned_hub()
        self.hub.record_scan("SC-L", "C1", "load", "W1-R1", version_no=1,
                             occurred_at=bootstrap.ts("07:30"))

    def tearDown(self) -> None:
        self.hub.close()

    def test_sequential_second_seal_conflicts(self):
        self.hub.seal_route("W1-R1", version_no=1, seal_no="SEAL-1",
                            operator="仓管甲", at=bootstrap.ts("07:50"))
        with self.assertRaises(SealConflictError):
            self.hub.seal_route("W1-R1", version_no=1, seal_no="SEAL-2",
                                operator="仓管乙", at=bootstrap.ts("07:51"))
        rows = self.hub.conn.execute("select * from active_seal").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seal_no"], "SEAL-1")

    def test_seal_requires_fully_loaded_manifest(self):
        hub = bootstrap.planned_hub()
        with self.assertRaises(StateError):
            hub.seal_route("W1-R1", version_no=1, seal_no="SEAL-X",
                           operator="仓管甲", at=bootstrap.ts("07:50"))
        hub.close()

    def test_seal_version_must_be_current(self):
        hub = bootstrap.planned_hub()
        diff_id = hub.submit_difference(
            "W1-R1",
            [{"change_type": "adjust", "store_id": "S1",
              "product_id": "apple", "qty": 3}],
            reason="门店改量", created_at=bootstrap.ts("07:20"))
        hub.approve_difference(diff_id, decided_by="调度甲", at=bootstrap.ts("07:25"))
        hub.record_scan("SC-L", "C1", "load", "W1-R1", version_no=2,
                        occurred_at=bootstrap.ts("07:30"))
        with self.assertRaises(VersionMismatchError):
            hub.seal_route("W1-R1", version_no=1, seal_no="SEAL-1",
                           operator="仓管甲", at=bootstrap.ts("07:50"))
        hub.seal_route("W1-R1", version_no=2, seal_no="SEAL-1",
                       operator="仓管甲", at=bootstrap.ts("07:51"))
        hub.close()

    def test_threads_race_leaves_exactly_one_seal(self):
        barrier = threading.Barrier(8)
        outcomes = []
        outcomes_lock = threading.Lock()

        def attempt(index: int) -> None:
            barrier.wait()
            try:
                self.hub.seal_route("W1-R1", version_no=1, seal_no=f"SEAL-{index}",
                                    operator=f"仓管{index}", at=bootstrap.ts("07:50"))
                with outcomes_lock:
                    outcomes.append("ok")
            except SealConflictError:
                with outcomes_lock:
                    outcomes.append("conflict")

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("conflict"), 7)
        rows = self.hub.conn.execute("select * from active_seal").fetchall()
        self.assertEqual(len(rows), 1)

    def test_two_connections_race_leaves_exactly_one_seal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "hub.db")
            hub1 = WaveHub(path)
            hub1.add_product("apple", "苹果", temp_zone="ambient",
                             coload_group="general", unit_weight=10)
            hub1.create_wave("W1", cutoff_at=bootstrap.CUTOFF)
            hub1.add_store("S1", "门店S1")
            hub1.set_store_window("S1", "W1", open_at=bootstrap.ts("09:00"),
                                  close_at=bootstrap.ts("12:00"))
            hub1.add_vehicle("V1", "沪A1", capacity=1000, temp_zones=["ambient"])
            hub1.add_container("C1")
            hub1.receive_order("O1", "S1", "W1", [{"product_id": "apple", "qty": 2}],
                               created_at="2026-09-23T09:00:00+08:00")
            hub1.cutoff_wave("W1", at=bootstrap.CUTOFF)
            hub1.plan_wave("W1", depart_at=bootstrap.ts("08:00"),
                          max_route_minutes=600)
            hub1.record_scan("SC-L", "C1", "load", "W1-R1", version_no=1,
                             occurred_at=bootstrap.ts("07:30"))
            hub2 = WaveHub(path)
            barrier = threading.Barrier(2)
            outcomes = []

            def attempt(hub, seal_no):
                barrier.wait()
                try:
                    hub.seal_route("W1-R1", version_no=1, seal_no=seal_no,
                                   operator="仓管", at=bootstrap.ts("07:50"))
                    outcomes.append("ok")
                except SealConflictError:
                    outcomes.append("conflict")

            t1 = threading.Thread(target=attempt, args=(hub1, "SEAL-A"))
            t2 = threading.Thread(target=attempt, args=(hub2, "SEAL-B"))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            self.assertEqual(sorted(outcomes), ["conflict", "ok"])
            check = WaveHub(path)
            rows = check.conn.execute("select * from active_seal").fetchall()
            self.assertEqual(len(rows), 1)
            hub1.close()
            hub2.close()
            check.close()


if __name__ == "__main__":
    unittest.main()
