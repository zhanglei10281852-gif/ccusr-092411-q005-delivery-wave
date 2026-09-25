"""波次编排引擎。

决定装车波次的五项约束（缺一不可，违反即拆波或拆单，并记录原因码）：
1. 门店收货窗口：逐店到达时刻必须落在 [open, close]；
2. 车辆载重：outbound 商品重量 + 回收箱筐皮重 ≤ 载重；
3. 温层：波次涉及的全部温层必须被车辆覆盖；
4. 商品共载：conflicts/coload_tags 互斥的商品不得同车；
5. 线路时长：行驶基线 + 逐店作业 ≤ 线路时长上限。
箱筐回收任务额外产生停靠店与皮重占用。

纯函数式：输入投影快照，输出 Plan，由应用层落事件。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .clock import parse_dt
from .errors import PlanningError, NO_ELIGIBLE_VEHICLE
from .models import Item

CONTAINER_TARE_KG = 2.0  # 回收空箱计入载重的皮重


@dataclass
class WaveSpec:
    wave_id: str
    route_id: str
    vehicle_id: str
    depart_at: str
    assignments: list[dict[str, Any]]
    order_ids: list[str]
    return_tasks: list[dict[str, Any]]
    load_weight: float
    reasons: list[str]
    delivery_version: str


@dataclass
class SplitSpec:
    parent_order_id: str
    child_order_id: str
    items: list[dict[str, Any]]
    reason: str
    wave_id: str | None = None


@dataclass
class Plan:
    waves: list[WaveSpec] = field(default_factory=list)
    splits: list[SplitSpec] = field(default_factory=list)


# ---------------------------------------------------------------- 工具

def _rebuild_items(lines: dict[str, dict[str, Any]]) -> list[Item]:
    return [
        Item(sku=l["sku"], qty=l["qty"], zone=l["zone"], weight=l["weight"],
             coload_tags=frozenset(l.get("coload_tags", [])),
             conflicts=frozenset(l.get("conflicts", [])))
        for l in lines.values() if l["qty"] > 0
    ]


def _order_weight(order: dict[str, Any]) -> float:
    return sum(l["weight"] * l["qty"] for l in order["items"].values())


def _order_zones(order: dict[str, Any]) -> frozenset[str]:
    return frozenset(l["zone"] for l in order["items"].values() if l["qty"] > 0)


def _coload_ok(wave_tags: frozenset[str], wave_conflicts: frozenset[str],
               order: dict[str, Any]) -> bool:
    items = _rebuild_items(order["items"])
    tags = frozenset().union(*(i.coload_tags for i in items)) if items else frozenset()
    conflicts = frozenset().union(*(i.conflicts for i in items)) if items else frozenset()
    return not (wave_conflicts & tags or conflicts & wave_tags)


# ---------------------------------------------------------------- 单车试算

@dataclass
class _Accum:
    vehicle: dict[str, Any]
    route_id: str
    orders: list[dict[str, Any]] = field(default_factory=list)
    return_tasks: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def stores(self) -> list[str]:
        stores = [o["store_id"] for o in self.orders]
        stores += [t["store_id"] for t in self.return_tasks
                   if t["store_id"] not in stores]
        return stores

    @property
    def weight(self) -> float:
        goods = sum(_order_weight(o) for o in self.orders)
        tare = sum(t.get("container_count", 0) for t in self.return_tasks) * CONTAINER_TARE_KG
        return goods + tare

    @property
    def zones(self) -> frozenset[str]:
        out: frozenset[str] = frozenset()
        for o in self.orders:
            out |= _order_zones(o)
        return out

    @property
    def tags(self) -> tuple[frozenset[str], frozenset[str]]:
        items: list[Item] = []
        for o in self.orders:
            items += _rebuild_items(o["items"])
        tags = frozenset().union(*(i.coload_tags for i in items)) if items else frozenset()
        conflicts = frozenset().union(*(i.conflicts for i in items)) if items else frozenset()
        return tags, conflicts

    def duration_minutes_for(self, vehicle: dict[str, Any], service_minutes: int) -> int:
        base = vehicle.get("base_minutes", {}).get(self.route_id, 0)
        return base + len(self.stores) * service_minutes

    def window_bounds_for(self, vehicle: dict[str, Any], service_minutes: int):
        """返回 (depart 下界, depart 上界)；不可行返回 None。

        窗口可行性取决于具体车辆的行驶基线，因此必须按候选车辆分别计算。
        近似但自洽：按窗口截止先后排店，到达第 i 店 = 发车 + 行驶基线 + i*作业时长。
        """
        from datetime import timedelta

        ordered = sorted(
            self.orders,
            key=lambda o: (parse_dt(o["window_close"]), parse_dt(o["window_open"])),
        )
        base = vehicle.get("base_minutes", {}).get(self.route_id, 0)
        lower = None
        upper = None
        for i, o in enumerate(ordered):
            transit = timedelta(minutes=base + i * service_minutes)
            lo = parse_dt(o["window_open"]) - transit
            hi = parse_dt(o["window_close"]) - transit
            lower = lo if lower is None else max(lower, lo)
            upper = hi if upper is None else min(upper, hi)
        if lower is None:
            return None
        return lower, upper


def _eligible_vehicles(acc: _Accum, candidates: list[dict[str, Any]],
                       service_minutes: int,
                       earliest_depart: Any = None) -> tuple[list[dict[str, Any]], str | None]:
    """从空/已有装载两种角度筛选车辆，返回 (可行车辆, 首个阻碍原因)。"""
    fit: list[dict[str, Any]] = []
    first_block: str | None = None
    for v in candidates:
        if not acc.zones <= v["zones"]:
            first_block = first_block or "ZONE_LIMIT"
            continue
        if acc.weight > v["capacity_kg"]:
            first_block = first_block or "CAPACITY_LIMIT"
            continue
        if acc.duration_minutes_for(v, service_minutes) > v["route_minutes_limit"]:
            first_block = first_block or "ROUTE_DURATION"
            continue
        bounds = acc.window_bounds_for(v, service_minutes)
        if bounds is not None:
            lower, upper = bounds
            if lower > upper:
                first_block = first_block or "WINDOW_LIMIT"
                continue
            if earliest_depart is not None and upper < earliest_depart:
                # 即便立刻发车也赶不上任何门店窗口
                first_block = first_block or "WINDOW_LIMIT"
                continue
        fit.append(v)
    return fit, first_block


# ---------------------------------------------------------------- 拆单

def _split_order(order: dict[str, Any], max_weight: float) -> dict[str, Any] | None:
    """按载重把订单拆成两坨；无法再拆返回 None。"""
    keep_lines: dict[str, dict[str, Any]] = {}
    move_lines: dict[str, dict[str, Any]] = {}
    budget = max_weight
    for l in sorted(order["items"].values(), key=lambda x: x["weight"] * x["qty"], reverse=True):
        total = l["weight"] * l["qty"]
        if total <= budget:
            keep_lines[l["sku"]] = dict(l)
            budget -= total
        else:
            take = min(l["qty"], int(budget // l["weight"])) if l["weight"] > 0 else 0
            if take > 0:
                kept = dict(l, qty=take)
                keep_lines[l["sku"]] = kept
                budget -= take * l["weight"]
                rest = l["qty"] - take
                if rest > 0:
                    move_lines.setdefault(l["sku"], dict(l, qty=0))
                    move_lines[l["sku"]]["qty"] += rest
            else:
                move_lines.setdefault(l["sku"], dict(l, qty=0))
                move_lines[l["sku"]]["qty"] += l["qty"]
    # 单件就超重、无法切分的行
    for l in order["items"].values():
        if l["weight"] > max_weight and l["qty"] > 0:
            return None
    if not move_lines:
        return None
    return {"keep": list(keep_lines.values()), "move": list(move_lines.values())}


# ---------------------------------------------------------------- 主编排

def build_plan(
    *,
    route_orders: dict[str, list[dict[str, Any]]],
    vehicles: list[dict[str, Any]],
    free_containers: dict[str, list[dict[str, Any]]],
    return_tasks: dict[str, list[dict[str, Any]]],
    wave_seq: dict[str, int] | None = None,
    earliest_depart: Any = None,
) -> Plan:
    plan = Plan()
    used_vehicles: set[str] = set()
    wave_seq = wave_seq or {}
    free_ptr = {r: list(cs) for r, cs in free_containers.items()}

    for route_id, orders in sorted(route_orders.items()):
        orders = sorted(
            orders,
            key=lambda o: (o["window_close"], -_order_weight(o)),
        )
        tasks = list(return_tasks.get(route_id, []))
        service_minutes = orders[0]["service_minutes"] if orders else 15
        pending = list(orders)
        route_wave_index = wave_seq.get(route_id, 0)

        while pending:
            candidates = [v for v in vehicles
                          if v["status"] == "active" and v["vehicle_id"] not in used_vehicles]
            acc = _Accum(vehicle={}, route_id=route_id)  # type: ignore[arg-type]
            acc.vehicle = {}  # 车辆在定载后选定
            acc.return_tasks = list(tasks)  # 回收任务挂在该线路首波
            tasks = []
            chosen: dict[str, Any] | None = None
            current_reasons: list[str] = []
            remaining: list[dict[str, Any]] = []

            for order in pending:
                trial = _Accum(
                    vehicle={}, route_id=route_id,
                    orders=acc.orders + [order],
                    return_tasks=acc.return_tasks,
                )
                tags, conflicts = acc.tags
                if not _coload_ok(tags, conflicts, order):
                    current_reasons.append("COLOADING_CONFLICT")
                    remaining.append(order)
                    continue
                fit, block = _eligible_vehicles(
                    trial, candidates, service_minutes, earliest_depart,
                )
                if fit:
                    acc.orders.append(order)
                    chosen = min(fit, key=lambda v: v["capacity_kg"])
                else:
                    # 单订单都装不进任何车：尝试按载重拆单
                    solo = _Accum(vehicle={}, route_id=route_id,
                                  orders=[order], return_tasks=acc.return_tasks)
                    solo_fit, solo_block = _eligible_vehicles(
                        solo, candidates, service_minutes, earliest_depart)
                    if not solo_fit and solo_block == "CAPACITY_LIMIT":
                        biggest = max(candidates, key=lambda v: v["capacity_kg"], default=None)
                        if biggest is not None:
                            cut = _split_order(
                                order,
                                biggest["capacity_kg"]
                                - sum(t.get("container_count", 0)
                                      for t in acc.return_tasks) * CONTAINER_TARE_KG,
                            )
                            if cut is not None:
                                route_wave_index += 1
                                child_id = f"{order['order_id']}-S{route_wave_index}"
                                plan.splits.append(SplitSpec(
                                    parent_order_id=order["order_id"],
                                    child_order_id=child_id,
                                    items=cut["move"],
                                    reason="CAPACITY_LIMIT",
                                ))
                                order["items"] = {l["sku"]: l for l in cut["keep"]}
                                # 拆出的部分作为子订单继续排队
                                remaining.append({
                                    "order_id": child_id,
                                    "store_id": order["store_id"],
                                    "route_id": route_id,
                                    "window_open": order["window_open"],
                                    "window_close": order["window_close"],
                                    "service_minutes": order["service_minutes"],
                                    "items": {l["sku"]: l for l in cut["move"]},
                                    "_synthetic": True,
                                })
                                if _order_weight(order) > 0:
                                    acc.orders.append(order)
                                    chosen = min(
                                        (v for v in candidates
                                         if acc.zones <= v["zones"]
                                         and acc.weight <= v["capacity_kg"]),
                                        key=lambda v: v["capacity_kg"], default=None,
                                    )
                                continue
                    current_reasons.append(block or "NO_ELIGIBLE_VEHICLE")
                    remaining.append(order)

            if not acc.orders:
                raise PlanningError(
                    NO_ELIGIBLE_VEHICLE,
                    f"线路 {route_id} 存在任何车辆都无法承运的需求：{current_reasons}",
                    route_id=route_id, reasons=current_reasons,
                )

            if chosen is None:
                fit, _ = _eligible_vehicles(acc, candidates, service_minutes,
                                            earliest_depart)
                chosen = min(fit, key=lambda v: v["capacity_kg"])
            used_vehicles.add(chosen["vehicle_id"])
            acc.vehicle = chosen

            bounds = acc.window_bounds_for(chosen, service_minutes)
            if bounds:
                lower, _upper = bounds
                if earliest_depart is not None and lower < earliest_depart:
                    lower = earliest_depart
                depart_dt = lower
            else:
                depart_dt = parse_dt(acc.orders[0]["window_open"])
            depart_at = depart_dt.isoformat()

            route_wave_index += 1
            wave_id = f"wave-{route_id}-{route_wave_index:02d}"
            containers = free_ptr.get(route_id, [])
            needed = len(acc.orders)
            if len(containers) < needed:
                raise PlanningError(
                    NO_ELIGIBLE_VEHICLE,
                    f"线路 {route_id} 空闲周转箱不足：需要 {needed}，可用 {len(containers)}",
                    route_id=route_id, needed=needed, available=len(containers),
                )
            assignments = []
            for order in acc.orders:
                c = containers.pop(0)
                assignments.append({
                    "container_id": c["container_id"],
                    "order_id": order["order_id"],
                    "store_id": order["store_id"],
                    "items": [dict(l) for l in order["items"].values()],
                })

            spec = WaveSpec(
                wave_id=wave_id,
                route_id=route_id,
                vehicle_id=chosen["vehicle_id"],
                depart_at=depart_at,
                assignments=assignments,
                order_ids=[o["order_id"] for o in acc.orders],
                return_tasks=acc.return_tasks,
                load_weight=round(acc.weight, 3),
                reasons=sorted(set(current_reasons)),
                delivery_version=f"{wave_id}@v1",
            )
            plan.waves.append(spec)

            # 因约束被排除的订单进入下一波；给下一波预留原因可见性
            pending = remaining

    return plan
