"""波次编排引擎。

输入截单后的门店需求、可用车辆、收货窗口、共载限制、箱筐回收任务，
按以下约束把需求编排成装车波次（线路集合）：

- 门店收货窗口：车辆到达时间必须落在窗口内，提前到达允许等待；
- 车辆载重和温层：车上任意一点的在载不得超过载重，商品温层必须被车辆支持；
- 商品共载限制：存在限制关系的商品组不得同车；
- 线路时长：含返程   返回仓库在内的全程不得超过上限；
- 箱筐回收任务：沿途回收的空箱筐计入在载模拟，并为无需求的回收门店安排停靠。

编排是确定性的：同样的输入永远得到同样的波次，便于回看与审计。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import ceil
from typing import Callable

from .errors import InfeasiblePlanError

DEPOT = "DEPOT"

TravelFn = Callable[[str, str], float]


def default_travel(_from: str, _to: str) -> float:
    """缺省行驶时间估计：任意两点之间 30 分钟。"""
    return 30.0


@dataclass(frozen=True)
class DemandLine:
    order_id: str
    store_id: str
    product_id: str
    qty: float
    weight: float
    temp_zone: str
    coload_group: str


@dataclass(frozen=True)
class VehicleSpec:
    vehicle_id: str
    capacity: float
    temp_zones: frozenset


@dataclass(frozen=True)
class StoreWindow:
    store_id: str
    open_at: datetime
    close_at: datetime


@dataclass
class StopPlan:
    store_id: str
    lines: list = field(default_factory=list)
    empties: int = 0
    eta: datetime | None = None

    @property
    def weight(self) -> float:
        return sum(line.weight for line in self.lines)


@dataclass
class RoutePlan:
    route_id: str
    vehicle: VehicleSpec
    stops: list = field(default_factory=list)


@dataclass
class Plan:
    routes: list
    splits: list          # [{"order_id", "reason", "route_ids"}]
    assignments: dict     # (route_id, store_id) -> [container_id, ...]


def build_plan(
    *,
    wave_id: str,
    demands: list,
    vehicles: list,
    windows: list,
    restrictions: dict,
    recovery: dict,
    container_ids: list,
    container_capacity: float,
    empty_container_weight: float,
    depart_at: datetime,
    max_route_minutes: float,
    service_minutes: float,
    travel: TravelFn | None = None,
) -> Plan:
    """编排一个波次，不可行时抛出 :class:`InfeasiblePlanError`。"""
    travel = travel or default_travel
    windows_by_store = {w.store_id: w for w in windows}
    issues: list[str] = []

    demands_by_store: dict[str, list] = {}
    for line in demands:
        demands_by_store.setdefault(line.store_id, []).append(line)
    for store_id in list(demands_by_store) + [s for s in recovery if recovery[s] > 0]:
        if store_id not in windows_by_store:
            issues.append(f"门店 {store_id} 缺少本波次收货窗口")
    if issues:
        raise InfeasiblePlanError("；".join(issues))

    def check_route(vehicle: VehicleSpec, stops: list) -> str | None:
        """校验整条线路，可行返回 None，否则返回原因。"""
        groups = set()
        onboard = 0.0
        for stop in stops:
            for line in stop.lines:
                if line.temp_zone not in vehicle.temp_zones:
                    return f"车辆温层不支持 {line.temp_zone}"
                groups.add(line.coload_group)
                onboard += line.weight
        for (group_a, group_b), reason in restrictions.items():
            if group_a in groups and group_b in groups:
                return f"商品共载限制：{reason or group_a + '×' + group_b}"
        if onboard > vehicle.capacity + 1e-9:
            return "车辆载重不足"
        clock = depart_at
        previous = DEPOT
        for stop in stops:
            clock = clock + timedelta(minutes=travel(previous, stop.store_id))
            window = windows_by_store[stop.store_id]
            if clock > window.close_at:
                return f"错过门店 {stop.store_id} 的收货窗口"
            if clock < window.open_at:
                clock = window.open_at
            clock = clock + timedelta(minutes=service_minutes)
            onboard -= stop.weight
            onboard += stop.empties * empty_container_weight
            if onboard > vehicle.capacity + 1e-9:
                return "回收箱筐超出车辆载重"
            previous = stop.store_id
        clock = clock + timedelta(minutes=travel(previous, DEPOT))
        if (clock - depart_at).total_seconds() / 60.0 > max_route_minutes + 1e-9:
            return "线路时长超限"
        return None

    routes: list[RoutePlan] = []
    used_vehicles: set[str] = set()
    order_routes: dict[str, list[str]] = {}
    splits: list[dict] = []
    route_seq = 0

    def candidate_vehicles(*, temp_zone: str | None, min_capacity: float) -> list:
        pool = [
            v for v in vehicles
            if v.vehicle_id not in used_vehicles
            and v.capacity >= min_capacity - 1e-9
            and (temp_zone is None or temp_zone in v.temp_zones)
        ]
        return sorted(pool, key=lambda v: (v.capacity, v.vehicle_id))

    def open_route(store_id: str, line: DemandLine | None, empties: int,
                   min_capacity: float, temp_zone: str | None):
        """为单个停靠点开新线路，返回 (RoutePlan | None, 失败原因)。"""
        nonlocal route_seq
        last_reason = None
        for vehicle in candidate_vehicles(temp_zone=temp_zone, min_capacity=min_capacity):
            stop = StopPlan(store_id, [line] if line else [], empties)
            reason = check_route(vehicle, [stop])
            if reason is None:
                route_seq += 1
                route = RoutePlan(f"{wave_id}-R{route_seq}", vehicle, [stop])
                routes.append(route)
                used_vehicles.add(vehicle.vehicle_id)
                return route, None
            last_reason = reason
        return None, last_reason or "没有满足温层与载重的可用车辆"

    stores_sorted = sorted(
        demands_by_store,
        key=lambda s: (windows_by_store[s].open_at, s),
    )
    for store_id in stores_sorted:
        lines = sorted(demands_by_store[store_id], key=lambda l: (l.product_id, l.order_id))
        for line in lines:
            placed_route = None
            reject_reasons: dict[str, str] = {}
            for route in routes:
                stop = next((s for s in route.stops if s.store_id == store_id), None)
                if stop is not None:
                    stop.lines.append(line)
                    reason = check_route(route.vehicle, route.stops)
                    if reason is None:
                        placed_route = route
                        break
                    stop.lines.pop()
                else:
                    new_stop = StopPlan(store_id, [line], recovery.get(store_id, 0))
                    route.stops.append(new_stop)
                    reason = check_route(route.vehicle, route.stops)
                    if reason is None:
                        placed_route = route
                        break
                    route.stops.pop()
                reject_reasons[route.route_id] = reason
            if placed_route is None:
                placed_route, open_reason = open_route(
                    store_id, line, recovery.get(store_id, 0),
                    min_capacity=line.weight, temp_zone=line.temp_zone,
                )
                if placed_route is None:
                    issues.append(
                        f"订单 {line.order_id} 商品 {line.product_id}：{open_reason}"
                    )
                    continue
            known = order_routes.setdefault(line.order_id, [])
            if placed_route.route_id not in known:
                if known:
                    splits.append({
                        "order_id": line.order_id,
                        "reason": reject_reasons.get(known[0]) or "可用车辆数量限制",
                        "route_ids": known + [placed_route.route_id],
                    })
                known.append(placed_route.route_id)

    # 无配送需求、仅有回收任务的门店也要安排停靠。
    for store_id in sorted(
        (s for s, n in recovery.items() if n > 0 and s not in demands_by_store),
        key=lambda s: (windows_by_store[s].open_at, s),
    ):
        empties = recovery[store_id]
        placed = False
        for route in routes:
            route.stops.append(StopPlan(store_id, [], empties))
            if check_route(route.vehicle, route.stops) is None:
                placed = True
                break
            route.stops.pop()
        if not placed:
            route, open_reason = open_route(
                store_id, None, empties,
                min_capacity=empties * empty_container_weight, temp_zone=None,
            )
            if route is None:
                issues.append(f"门店 {store_id} 的箱筐回收任务：{open_reason}")

    if issues:
        raise InfeasiblePlanError("；".join(issues))

    # 最终按停靠顺序计算 ETA，并分配周转箱。
    pool = sorted(container_ids)
    assignments: dict[tuple[str, str], list[str]] = {}
    for route in routes:
        clock = depart_at
        previous = DEPOT
        for stop in route.stops:
            clock = clock + timedelta(minutes=travel(previous, stop.store_id))
            window = windows_by_store[stop.store_id]
            if clock < window.open_at:
                clock = window.open_at
            stop.eta = clock
            clock = clock + timedelta(minutes=service_minutes)
            previous = stop.store_id
            needed = ceil(stop.weight / container_capacity) if stop.weight > 0 else 0
            if needed > len(pool):
                raise InfeasiblePlanError(
                    f"周转箱不足：线路 {route.route_id} 门店 {stop.store_id} 需要 {needed} 只"
                )
            if needed:
                assignments[(route.route_id, stop.store_id)] = pool[:needed]
                pool = pool[needed:]
    return Plan(routes=routes, splits=splits, assignments=assignments)
