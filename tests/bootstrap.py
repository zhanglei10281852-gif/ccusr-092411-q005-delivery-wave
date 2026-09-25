"""测试公共装置：保证项目根目录可导入，并提供统一的商品/门店/时间助手。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wavehub import WaveHub  # noqa: E402

DAY = "2026-09-24"
CUTOFF = "2026-09-23T18:00:00+08:00"


def ts(clock: str, day: str = DAY) -> str:
    """'07:30' -> '2026-09-24T07:30:00+08:00'"""
    return f"{day}T{clock}:00+08:00"


def new_hub(db_path: str = ":memory:") -> WaveHub:
    hub = WaveHub(db_path)
    hub.add_product("apple", "苹果", temp_zone="ambient",
                    coload_group="general", unit_weight=10)
    hub.add_product("soap", "洗洁精", temp_zone="ambient",
                    coload_group="chemical", unit_weight=5)
    hub.add_product("fish", "冻鱼", temp_zone="frozen",
                    coload_group="seafood", unit_weight=20)
    hub.add_product("clam", "花蛤", temp_zone="chilled",
                    coload_group="seafood", unit_weight=10)
    hub.add_coload_restriction("seafood", "chemical", reason="生鲜与化学品禁止共载")
    return hub


def add_store(hub: WaveHub, store_id: str, wave_id: str,
              window: tuple = ("09:00", "12:00")) -> None:
    hub.add_store(store_id, f"门店{store_id}")
    hub.set_store_window(store_id, wave_id,
                         open_at=ts(window[0]), close_at=ts(window[1]))


def planned_hub() -> WaveHub:
    """两条线路的既定场景：R1 送 S1（常温苹果），R2 送 S2（冷冻鱼）。"""
    hub = new_hub()
    hub.create_wave("W1", cutoff_at=CUTOFF)
    add_store(hub, "S1", "W1")
    add_store(hub, "S2", "W1")
    hub.add_vehicle("V1", "沪A001", capacity=1000, temp_zones=["ambient"])
    hub.add_vehicle("V2", "沪A002", capacity=1000, temp_zones=["frozen"])
    hub.add_container("C1")
    hub.add_container("C2")
    hub.receive_order("O1", "S1", "W1", [{"product_id": "apple", "qty": 2}],
                      created_at="2026-09-23T09:00:00+08:00")
    hub.receive_order("O2", "S2", "W1", [{"product_id": "fish", "qty": 2}],
                      created_at="2026-09-23T09:05:00+08:00")
    hub.cutoff_wave("W1", at=CUTOFF)
    hub.plan_wave("W1", depart_at=ts("08:00"), max_route_minutes=600)
    return hub
