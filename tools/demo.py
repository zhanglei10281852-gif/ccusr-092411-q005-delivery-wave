"""端到端演示：用一条时间线串起配送负责人最关心的混乱场景。

运行：python3 tools/demo.py
纯内存运行，不落盘；每一步都打印发生了什么、系统为什么接受或拒绝。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wavehub import (
    BusinessRuleError, Item, ReturnTask, StoreOrder, Vehicle, WaveHub,
)
from wavehub import errors as E

T = "2026-09-22"


def line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_reject(label: str, fn) -> None:
    try:
        fn()
    except BusinessRuleError as exc:
        print(f"  [拒绝] {label} -> {exc.reason}：{exc}")
    else:
        raise AssertionError(f"{label} 本应被拒绝")


def main() -> None:
    hub = WaveHub(":memory:")

    line("1. 建档：车辆（含温层/载重/线路时长）、门店需求、周转箱、箱筐回收任务")
    hub.register_vehicle(
        Vehicle("v-01", "京A1001", 800.0,
                frozenset({"ambient", "chilled", "frozen"}), 480,
                {"R1": 60, "R2": 40}),
        f"{T}T06:00:00+08:00")
    hub.register_vehicle(
        Vehicle("v-02", "京A1002", 800.0,
                frozenset({"ambient", "chilled"}), 480, {"R1": 55}),
        f"{T}T06:01:00+08:00")
    hub.register_order(StoreOrder(
        "ord-101", "store-01", "R1",
        (Item("icecream", 20, "frozen", 2.0),
         Item("noodle", 100, "ambient", 0.5, coload_tags=frozenset({"food"}))),
        f"{T}T09:00:00+08:00", f"{T}T12:00:00+08:00"),
        f"{T}T06:10:00+08:00")
    hub.register_order(StoreOrder(
        "ord-102", "store-02", "R1",
        (Item("paper-towel", 10, "ambient", 1.0),),
        f"{T}T09:30:00+08:00", f"{T}T12:30:00+08:00"),
        f"{T}T06:11:00+08:00")
    hub.register_order(StoreOrder(
        "ord-201", "store-21", "R2",
        (Item("battery", 4, "ambient", 1.0),),
        f"{T}T09:30:00+08:00", f"{T}T13:00:00+08:00"),
        f"{T}T06:12:00+08:00")
    for cid in ("box-1", "box-2", "box-3", "box-9"):
        hub.register_container(cid, "R1", f"{T}T06:20:00+08:00")
    hub.register_container("box-X", "R2", f"{T}T06:21:00+08:00")
    hub.receive_return_task(ReturnTask("store-09", "R1", 6),
                            f"{T}T06:30:00+08:00")

    line("2. 编排波次：窗口/载重/温层/共载/时长/回收任务共同决定")
    plan = hub.plan_waves(f"{T}T07:00:00+08:00")
    for w in plan["waves"]:
        print(f"  波次 {w['wave_id']}：车 {w['vehicle_id']}，"
              f"装载 {w['load_weight']}kg（含回收皮重），"
              f"容器 {[a['container_id'] for a in w['assignments']]}，"
              f"版本 {w['delivery_version']}，拆/拒原因 {w['reasons']}")
    w1 = next(w["wave_id"] for w in plan["waves"] if w["wave_id"].endswith("R1-01"))

    line("3. 截单；司机领单；仓库装箱")
    hub.cutoff_wave(w1, f"{T}T07:30:00+08:00")
    hub.assign_driver(w1, "driver-ma", f"{T}T07:40:00+08:00")
    box1 = plan["waves"][0]["assignments"][0]["container_id"]
    box2 = plan["waves"][0]["assignments"][1]["container_id"]
    hub.load_container("scan-001", box1, f"{T}T07:50:00+08:00")
    print(f"  {box1} 已装车")

    show_reject(
        "扫到属于另一条线路清单的周转箱",
        lambda: hub.load_container("scan-wrong", "box-X",
                                   f"{T}T07:52:00+08:00", wave_id=w1),
    )
    show_reject(
        "同一箱重复装车（换新 scan_id 也不行）",
        lambda: hub.load_container("scan-002", box1, f"{T}T07:53:00+08:00"),
    )
    hub.load_container("scan-003", box2, f"{T}T07:55:00+08:00")
    print(f"  {box2} 已装车，同一 scan_id 重传结果：",
          hub.load_container("scan-003", box2, f"{T}T08:00:00+08:00"))

    line("4. 封签；并发/重复封签只留一个结果")
    sealed = hub.seal_vehicle(w1, "driver-ma", f"{T}T08:20:00+08:00")
    print("  封签版本：", sealed["delivery_version"])
    show_reject("再次封签",
                lambda: hub.seal_vehicle(w1, "driver-ma", f"{T}T08:21:00+08:00"))
    show_reject("封签后补扫装车",
                lambda: hub.load_container("scan-late", box1,
                                           f"{T}T09:00:00+08:00"))

    line("5. 封签后的改单：只有获批差异单也不能动已封签部分（按业务时间判）")
    aid = hub.request_adjustment(
        w1, "remove_container", f"{T}T08:10:00+08:00",
        detail={"container_id": box2}, reason="门店临时歇业")
    hub.decide_adjustment(aid, True, f"{T}T08:15:00+08:00", approver="boss")
    show_reject(f"差异单 {aid} 业务时间晚于封签 -> 拒绝",
                lambda: hub.apply_adjustment(aid, f"{T}T08:35:00+08:00"))

    line("6. 门店签收：错门店/重复签收拒绝；缺货与延迟对门店可见")
    show_reject("错门店签收",
                lambda: hub.sign_container("ss-wrong", box1, "store-02",
                                           f"{T}T10:00:00+08:00"))
    hub.sign_container("ss-001", box1, "store-01", f"{T}T10:05:00+08:00")
    hub.report_shortage("ord-101", "icecream", 20, 18, E.NOT_LOADED,
                        f"{T}T10:06:00+08:00", wave_id=w1, container_id=box1)
    status = hub.store_status("store-01")
    for o in status["orders"]:
        if o["shortages"]:
            print(f"  订单 {o['order_id']} 缺货明细：{o['shortages']}")

    line("7. 车辆故障 -> 整车转派（重校温层/载重），已交接容器逐件追踪")
    hub.report_breakdown("v-01", f"{T}T10:30:00+08:00")
    # v-02 无 frozen 温层
    show_reject("接替车温层不覆盖",
                lambda: hub.reassign_vehicle(w1, "v-02", "driver-x",
                                             f"{T}T10:40:00+08:00"))
    hub.register_vehicle(
        Vehicle("v-03", "京A1003", 900.0,
                frozenset({"ambient", "chilled", "frozen"}), 480, {"R1": 55}),
        f"{T}T10:41:00+08:00")
    out = hub.reassign_vehicle(w1, "v-03", "driver-x", f"{T}T10:45:00+08:00")
    print("  转派后新版本：", out["delivery_version"])
    trace = hub.container_trace(box1)
    print(f"  {box1} 交接链：", [c["action"] for c in trace["custody"]])
    print("  注意：box-1 已签收交接给门店，不随车转移；box-2 仍在车上并逐件留痕")
    trace2 = hub.container_trace(box2)
    print(f"  {box2} 交接链：", [(c["action"], c.get("to_vehicle"))
                               for c in trace2["custody"]])

    line("8. 仓库回看：改单与封签谁先谁后，时间线直接给出")
    for e in hub.wave_timeline(w1):
        print(f"  {e['occurred_at']}  {e['event_type']:<24} {e['aggregate_id']}")

    line("9. 离线扫描补传：按业务发生时间排序校验，重复扫描幂等")
    report = hub.upload_scans([
        {"scan_id": "off-1", "type": "sign", "container_id": box2,
         "store_id": "store-02", "occurred_at": f"{T}T11:10:00+08:00"},
        {"scan_id": "off-0", "type": "load", "container_id": box2,
         "occurred_at": f"{T}T07:00:00+08:00"},   # 早于截单 -> 拒绝
        {"scan_id": "off-1", "type": "sign", "container_id": box2,
         "store_id": "store-02", "occurred_at": f"{T}T11:30:00+08:00"},  # 幂等
    ])
    for r in report:
        print(f"  {r['scan_id']} {r['type']:<5} {r['status']:<9} "
              f"{r.get('reason', '')}")

    line("10. 恢复运行后巡检：待转派/未回收容器仍在正确环节")
    pending = hub.pending_recovery()
    print("  在途波次：", [(w["wave_id"], w["state"]) for w in pending["waves"]])
    print("  未回收容器：",
          [(c["container_id"], c["state"]) for c in pending["unreturned_containers"]])
    print("\n演示完成。")


if __name__ == "__main__":
    main()
