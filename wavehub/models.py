"""领域输入模型：门店需求、车辆与箱筐回收任务。

这些是命令侧的输入结构；系统内部状态由投影（projection.py）持有。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Item:
    """订单行：商品、数量、温层与共载标签。

    zone:        要求温层，例如 frozen / chilled / ambient
    coload_tags: 共载限制标签；标签同时出现在某商品的 conflicts 中即不可同车。
    conflicts:   本商品禁止同车的标签。
    """

    sku: str
    qty: int
    zone: str
    weight: float
    coload_tags: frozenset[str] = frozenset()
    conflicts: frozenset[str] = frozenset()

    def compatible_with(self, other: "Item") -> bool:
        return not (self.conflicts & other.coload_tags or other.conflicts & self.coload_tags)


@dataclass(frozen=True)
class StoreOrder:
    order_id: str
    store_id: str
    route_id: str
    items: tuple[Item, ...]
    window_open: str          # 门店收货窗口起（带时区 ISO 8601）
    window_close: str         # 门店收货窗口止
    service_minutes: int = 15  # 门店作业时长

    @property
    def total_weight(self) -> float:
        return sum(i.weight * i.qty for i in self.items)

    @property
    def zones(self) -> frozenset[str]:
        return frozenset(i.zone for i in self.items)


@dataclass(frozen=True)
class Vehicle:
    vehicle_id: str
    plate: str
    capacity_kg: float
    zones: frozenset[str]                  # 车辆可承运温层
    route_minutes_limit: int               # 线路时长上限
    base_minutes: dict[str, int] = field(default_factory=dict)  # route_id -> 行驶分钟
    status: str = "active"                 # active / broken

    def serves_zone(self, zone: str) -> bool:
        return zone in self.zones

    def route_minutes(self, route_id: str, stops: int, service_minutes: int) -> int:
        return self.base_minutes.get(route_id, 0) + stops * service_minutes


@dataclass(frozen=True)
class ReturnTask:
    """箱筐回收任务：某门店有待回收容器，计入波次的停靠与容器计划。"""

    store_id: str
    route_id: str
    container_count: int
