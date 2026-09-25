"""端到端示例：从需求收集到签收回收的完整波次生命周期。

运行：python3 examples/end_to_end.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wavehub import WaveHub  # noqa: E402

DAY = "2026-09-24"


def ts(clock: str) -> str:
    return f"{DAY}T{clock}:00+08:00"


def main() -> None:
    hub = WaveHub(":memory:")
    # 主数据：商品、温层、共载限制、车辆、周转箱
    hub.add_product("apple", "苹果", temp_zone="ambient",
                    coload_group="general", unit_weight=10)
    hub.add_product("fish", "冻鱼", temp_zone="frozen",
                    coload_group="seafood", unit_weight=20)
    hub.add_product("soap", "洗洁精", temp_zone="ambient",
                    coload_group="chemical", unit_weight=5)
    hub.add_coload_restriction("seafood", "chemical", reason="生鲜与化学品禁止共载")
    hub.add_vehicle("V1", "沪A001", capacity=1000, temp_zones=["ambient"])
    hub.add_vehicle("V2", "沪A002", capacity=1000, temp_zones=["frozen"])
    for i in range(1, 5):
        hub.add_container(f"C{i}")

    # 波次与收货窗口
    hub.create_wave("W1", cutoff_at="2026-09-23T18:00:00+08:00")
    for store_id, window in (("S1", ("09:00", "11:00")), ("S2", ("09:30", "12:00"))):
        hub.add_store(store_id, f"门店{store_id}")
        hub.set_store_window(store_id, "W1", open_at=ts(window[0]),
                             close_at=ts(window[1]))
    hub.add_recovery_task("S1", "W1", 1)  # S1 有一只空箱要回收

    # 截单前：需求自动合并
    hub.receive_order("O1", "S1", "W1", [{"product_id": "apple", "qty": 3}],
                      created_at="2026-09-23T09:00:00+08:00")
    merged = hub.receive_order("O2", "S1", "W1",
                               [{"product_id": "apple", "qty": 2},
                                {"product_id": "soap", "qty": 4}],
                               created_at="2026-09-23T10:00:00+08:00")
    print(f"截单前合并：O2 并入 {merged}")
    hub.receive_order("O3", "S2", "W1", [{"product_id": "fish", "qty": 2}],
                      created_at="2026-09-23T11:00:00+08:00")

    # 截单与编排
    hub.cutoff_wave("W1", at="2026-09-23T18:00:00+08:00")
    plan = hub.plan_wave("W1", depart_at=ts("08:00"), max_route_minutes=360)
    for route in plan["routes"]:
        stops = "、".join(f"{s['store_id']}@{s['eta'][11:16]}" for s in route["stops"])
        print(f"编排：{route['route_id']} 车辆 {route['vehicle_id']} 停靠 {stops}")

    # 截单后：获批差异单改变未封签部分
    diff = hub.submit_difference(
        "W1-R1",
        [{"change_type": "adjust", "store_id": "S1",
          "product_id": "apple", "qty": 6}],
        reason="门店节前加单", created_at=ts("07:00"))
    version = hub.approve_difference(diff, decided_by="调度甲", at=ts("07:05"))
    print(f"差异单 {diff} 获批，W1-R1 进入配送版本 v{version}")

    # 司机领单、仓库装箱、车辆封签（同一配送版本）
    manifest = hub.pull_manifest("W1-R1", pulled_by="司机乙", at=ts("07:10"))
    print(f"司机领单：W1-R1 v{manifest['version_no']}，"
          f"周转箱 {manifest['stops'][0]['containers']}")
    box = manifest["stops"][0]["containers"][0]
    wrong = hub.record_scan("SC-ERR", box, "load", "W1-R2", version_no=1,
                            occurred_at=ts("07:20"))
    print(f"跨线装车拦截：{wrong['result']} —— {wrong['reason']}")
    hub.record_scan("SC-1", box, "load", "W1-R1", version_no=2,
                    occurred_at=ts("07:30"))
    hub.seal_route("W1-R1", version_no=2, seal_no="SEAL-001",
                   operator="仓管丙", at=ts("07:50"))
    print("封签完成：SEAL-001（并发封签只会保留一个结果）")
    hub.depart_route("W1-R1", at=ts("08:00"))

    # 门店签收与空箱回收
    hub.record_scan("SC-2", box, "handover", "W1-R1", version_no=2,
                    store_id="S1", occurred_at=ts("09:30"))
    dup = hub.record_scan("SC-2", box, "handover", "W1-R1", version_no=2,
                          store_id="S1", occurred_at=ts("09:30"))
    print(f"重复签收补传：{dup['result']}（不会再次生效）")
    hub.record_scan("SC-3", box, "recover", "W1-R1", version_no=2,
                    store_id="S1", occurred_at=ts("10:00"))

    # 门店查询：缺货/拆单/延迟原因
    view = hub.store_view("S1", "W1")
    stop = view["stops"][0]
    print(f"门店视角：{stop['route_id']} 已签收={stop['signed']}，"
          f"商品行 {[(l['product_id'], l['qty_planned']) for l in view['lines']]}")

    # 恢复运行报告
    report = hub.recovery_report()
    print(f"恢复报告：待转派 {len(report['pending_transfers'])} 条线路，"
          f"未回收容器 {[c['container_id'] for c in report['unrecovered_containers']]}")
    hub.close()


if __name__ == "__main__":
    main()
