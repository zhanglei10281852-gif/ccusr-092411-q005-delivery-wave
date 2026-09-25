"""扫描交接：跨线装车拦截、幂等、离线补传按业务发生时间校验。"""
from __future__ import annotations

import unittest

import bootstrap


class ScanningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = bootstrap.planned_hub()

    def tearDown(self) -> None:
        self.hub.close()

    def _load_seal_depart(self, route_id="W1-R1", container="C1"):
        self.hub.record_scan("SC-L", container, "load", route_id, version_no=1,
                             occurred_at=bootstrap.ts("07:30"))
        self.hub.seal_route(route_id, version_no=1, seal_no=f"SEAL-{route_id}",
                            operator="仓管丙", at=bootstrap.ts("07:50"))
        self.hub.depart_route(route_id, at=bootstrap.ts("08:00"))

    def test_full_handover_cycle(self):
        hub = self.hub
        self._load_seal_depart()
        result = hub.record_scan("SC-H", "C1", "handover", "W1-R1", version_no=1,
                                 store_id="S1", occurred_at=bootstrap.ts("09:30"))
        self.assertEqual(result["result"], "accepted")
        container = hub.conn.execute(
            "select * from container where container_id = 'C1'").fetchone()
        self.assertEqual(container["status"], "handed_over")
        self.assertEqual(container["current_store_id"], "S1")
        result = hub.record_scan("SC-R", "C1", "recover", "W1-R1", version_no=1,
                                 store_id="S1", occurred_at=bootstrap.ts("10:00"))
        self.assertEqual(result["result"], "accepted")
        result = hub.record_scan("SC-B", "C1", "return", "W1-R1", version_no=1,
                                 occurred_at=bootstrap.ts("11:00"))
        self.assertEqual(result["result"], "accepted")
        container = hub.conn.execute(
            "select * from container where container_id = 'C1'").fetchone()
        self.assertEqual(container["status"], "at_warehouse")
        self.assertIsNone(container["current_route_id"])
        events = [row["event_type"] for row in hub.conn.execute(
            "select event_type from event_log order by rowid")]
        for expected in ("container.loaded", "container.handed_over",
                         "store.signed", "container.recovered", "container.returned"):
            self.assertIn(expected, events)

    def test_container_from_another_route_is_rejected(self):
        """司机到门店才发现周转箱属于另一条线路 —— 装车环节就应当拦下。"""
        result = self.hub.record_scan(
            "SC-X", "C1", "load", "W1-R2", version_no=1,
            occurred_at=bootstrap.ts("07:30"))
        self.assertEqual(result["result"], "rejected")
        self.assertIn("W1-R1", result["reason"])
        container = self.hub.conn.execute(
            "select status from container where container_id = 'C1'").fetchone()
        self.assertEqual(container["status"], "at_warehouse")
        # 拒绝同样留痕，仓库回看可见
        scan = self.hub.conn.execute(
            "select result, reason from scan_event where scan_id = 'SC-X'").fetchone()
        self.assertEqual(scan["result"], "rejected")

    def test_same_scan_id_reupload_is_duplicate(self):
        self.hub.record_scan("SC-1", "C1", "load", "W1-R1", version_no=1,
                             occurred_at=bootstrap.ts("07:30"))
        again = self.hub.record_scan("SC-1", "C1", "load", "W1-R1", version_no=1,
                                     occurred_at=bootstrap.ts("07:30"),
                                     received_at=bootstrap.ts("09:00"))
        self.assertEqual(again["result"], "duplicate")
        accepted = self.hub.conn.execute(
            "select count(*) as n from scan_event"
            " where container_id = 'C1' and result = 'accepted'").fetchone()
        self.assertEqual(accepted["n"], 1)

    def test_double_scan_does_not_load_twice(self):
        self.hub.record_scan("SC-1", "C1", "load", "W1-R1", version_no=1,
                             occurred_at=bootstrap.ts("07:30"))
        dup = self.hub.record_scan("SC-2", "C1", "load", "W1-R1", version_no=1,
                                   occurred_at=bootstrap.ts("07:31"))
        self.assertEqual(dup["result"], "duplicate")

    def test_double_sign_does_not_sign_twice(self):
        self._load_seal_depart()
        self.hub.record_scan("SC-H1", "C1", "handover", "W1-R1", version_no=1,
                             store_id="S1", occurred_at=bootstrap.ts("09:30"))
        dup = self.hub.record_scan("SC-H2", "C1", "handover", "W1-R1", version_no=1,
                                   store_id="S1", occurred_at=bootstrap.ts("09:35"))
        self.assertEqual(dup["result"], "duplicate")
        accepted = self.hub.conn.execute(
            "select count(*) as n from scan_event where container_id = 'C1'"
            " and action = 'handover' and result = 'accepted'").fetchone()
        self.assertEqual(accepted["n"], 1)

    def test_offline_scans_validated_by_business_time(self):
        """离线补传：回收扫描先到、签收后传，按发生时间校验，重传后收敛。"""
        hub = self.hub
        self._load_seal_depart()
        # 门店离线，回收扫描（10:00 发生）比签收扫描（09:30 发生）先上传
        early = hub.record_scan("SC-R", "C1", "recover", "W1-R1", version_no=1,
                                store_id="S1", occurred_at=bootstrap.ts("10:00"),
                                received_at=bootstrap.ts("10:05"))
        self.assertEqual(early["result"], "rejected")
        handover = hub.record_scan("SC-H", "C1", "handover", "W1-R1", version_no=1,
                                   store_id="S1", occurred_at=bootstrap.ts("09:30"),
                                   received_at=bootstrap.ts("10:10"))
        self.assertEqual(handover["result"], "accepted")
        retry = hub.record_scan("SC-R", "C1", "recover", "W1-R1", version_no=1,
                                store_id="S1", occurred_at=bootstrap.ts("10:00"),
                                received_at=bootstrap.ts("10:15"))
        self.assertEqual(retry["result"], "accepted")
        container = hub.conn.execute(
            "select status from container where container_id = 'C1'").fetchone()
        self.assertEqual(container["status"], "return_loaded")

    def test_handover_earlier_than_departure_is_rejected(self):
        hub = self.hub
        self._load_seal_depart()
        result = hub.record_scan("SC-H", "C1", "handover", "W1-R1", version_no=1,
                                 store_id="S1", occurred_at=bootstrap.ts("07:59"))
        self.assertEqual(result["result"], "rejected")
        self.assertIn("发车", result["reason"])

    def test_load_after_seal_is_rejected(self):
        hub = self.hub
        hub.record_scan("SC-1", "C1", "load", "W1-R1", version_no=1,
                        occurred_at=bootstrap.ts("07:30"))
        hub.seal_route("W1-R1", version_no=1, seal_no="SEAL-1",
                       operator="仓管丙", at=bootstrap.ts("07:50"))
        late = hub.record_scan("SC-2", "C2", "load", "W1-R1", version_no=1,
                               occurred_at=bootstrap.ts("07:55"))
        self.assertEqual(late["result"], "rejected")


if __name__ == "__main__":
    unittest.main()
