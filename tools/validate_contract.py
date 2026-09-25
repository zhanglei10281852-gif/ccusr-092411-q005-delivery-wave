"""校验领域合同与样例事件，不实现业务服务。

自动选取 domain/ 下版本号最高的合同（contract.json 视为 v1），
样例事件的事件类型必须被最新合同允许；新版本只能追加，不得删除旧声明。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_json(relative_path: str):
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def latest_contract() -> tuple[dict, int]:
    contracts = [(1, ROOT / "domain" / "contract.json")]
    for path in (ROOT / "domain").glob("contract.v*.json"):
        m = re.search(r"contract\.v(\d+)\.json$", path.name)
        if m:
            contracts.append((int(m.group(1)), path))
    version, path = max(contracts, key=lambda x: x[0])
    return json.loads(path.read_text(encoding="utf-8")), version


def validate() -> tuple[int, int]:
    contract, version = latest_contract()
    events = load_json("examples/events.json")
    required = {"project", "entities", "states", "event_types", "time_policy"}
    missing = sorted(required - set(contract))
    if missing:
        raise ValueError("领域合同缺少字段：" + "、".join(missing))
    if contract["time_policy"] != "ISO 8601 with timezone":
        raise ValueError("time_policy 必须明确包含时区")
    if version >= 2:
        for key in ("rules", "reason_codes", "container_states"):
            if key not in contract:
                raise ValueError(f"v{version} 合同缺少字段：{key}")
        for rule in ("wave_planning", "cutoff", "delivery_version", "offline_scan",
                     "idempotency", "concurrent_seal", "reassignment",
                     "store_visibility", "recovery"):
            if rule not in contract["rules"]:
                raise ValueError(f"v{version} 合同缺少规则声明：{rule}")

    allowed = set(contract["event_types"])
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "create table event_log(event_id text primary key, event_type text not null, "
        "aggregate_id text not null, occurred_at text not null)"
    )
    previous = None
    for event in events:
        if event["event_type"] not in allowed:
            raise ValueError(f"未知事件类型：{event['event_type']}")
        occurred_at = datetime.fromisoformat(event["occurred_at"])
        if occurred_at.tzinfo is None:
            raise ValueError("样例事件必须包含时区")
        if previous is not None and occurred_at < previous:
            raise ValueError("样例事件必须按发生时间排序")
        previous = occurred_at
        connection.execute(
            "insert into event_log values (?, ?, ?, ?)",
            (event["event_id"], event["event_type"], event["aggregate_id"], event["occurred_at"]),
        )
    connection.commit()
    stored = connection.execute("select count(*) from event_log").fetchone()[0]
    connection.close()
    return len(contract["entities"]), stored


if __name__ == "__main__":
    entity_count, event_count = validate()
    print(f"合同校验通过：{entity_count} 类实体，{event_count} 条样例事件")
