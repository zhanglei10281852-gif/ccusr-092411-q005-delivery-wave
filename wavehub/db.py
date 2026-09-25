"""SQLite 存储层：建表语句与连接工厂。

所有业务状态都持久化在 SQLite 中，进程重启后重新打开同一文件即可恢复：
待转派车辆、未回收容器、配送版本与扫描记录都处在正确的环节上。
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
create table if not exists store (
    store_id    text primary key,
    name        text not null
);

create table if not exists store_window (
    store_id    text not null,
    wave_id     text not null,
    open_at     text not null,
    close_at    text not null,
    primary key (store_id, wave_id)
);

create table if not exists product (
    product_id   text primary key,
    name         text not null,
    temp_zone    text not null,
    coload_group text not null,
    unit_weight  real not null
);

create table if not exists coload_restriction (
    group_a  text not null,
    group_b  text not null,
    reason   text not null default '',
    primary key (group_a, group_b)
);

create table if not exists vehicle (
    vehicle_id text primary key,
    plate      text not null,
    capacity   real not null,
    temp_zones text not null,
    status     text not null default 'available'
);

create table if not exists container (
    container_id     text primary key,
    kind             text not null default 'turnover_box',
    status           text not null default 'at_warehouse',
    current_route_id text,
    current_store_id text
);

create table if not exists wave (
    wave_id            text primary key,
    cutoff_at          text not null,
    status             text not null default 'collecting',
    depart_at          text,
    max_route_minutes  integer,
    service_minutes    integer not null default 10,
    container_capacity real
);

create table if not exists store_order (
    order_id    text primary key,
    store_id    text not null,
    wave_id     text not null,
    status      text not null default 'open',
    merged_into text,
    created_at  text not null
);

create table if not exists order_line (
    order_id   text not null,
    product_id text not null,
    qty        real not null,
    primary key (order_id, product_id)
);

create table if not exists recovery_task (
    store_id text not null,
    wave_id  text not null,
    empties  integer not null,
    primary key (store_id, wave_id)
);

create table if not exists route (
    route_id        text primary key,
    wave_id         text not null,
    vehicle_id      text not null,
    seq_no          integer not null,
    status          text not null default 'planned',
    current_version integer not null default 0,
    delay_minutes   integer not null default 0,
    delay_reason    text,
    departed_at     text,
    created_at      text not null
);

create table if not exists route_stop (
    route_id text not null,
    seq      integer not null,
    store_id text not null,
    eta      text not null,
    primary key (route_id, seq)
);

create table if not exists delivery_version (
    route_id   text not null,
    version_no integer not null,
    cause      text not null,
    status     text not null,
    created_at text not null,
    primary key (route_id, version_no)
);

create table if not exists version_line (
    route_id   text not null,
    version_no integer not null,
    store_id   text not null,
    order_id   text not null,
    product_id text not null,
    qty        real not null,
    primary key (route_id, version_no, store_id, product_id)
);

create table if not exists container_assignment (
    route_id     text not null,
    version_no   integer not null,
    container_id text not null,
    store_id     text not null,
    primary key (route_id, version_no, container_id)
);

create table if not exists difference_order (
    diff_id         text primary key,
    route_id        text not null,
    reason          text not null,
    status          text not null default 'pending',
    created_at      text not null,
    decided_by      text,
    decided_at      text,
    applied_version integer
);

create table if not exists difference_line (
    diff_id     text not null,
    line_no     integer not null,
    change_type text not null,
    store_id    text not null,
    product_id  text not null,
    qty         real not null,
    primary key (diff_id, line_no)
);

create table if not exists scan_event (
    scan_id      text primary key,
    container_id text not null,
    action       text not null,
    route_id     text not null,
    store_id     text,
    version_no   integer,
    occurred_at  text not null,
    received_at  text not null,
    result       text not null,
    reason       text
);

create table if not exists active_seal (
    route_id   text primary key,
    version_no integer not null,
    seal_no    text not null unique,
    sealed_at  text not null,
    operator   text not null
);

create table if not exists seal_history (
    route_id    text not null,
    version_no  integer not null,
    seal_no     text not null,
    sealed_at   text not null,
    operator    text not null,
    voided_at   text not null,
    void_reason text not null
);

create table if not exists transfer (
    transfer_id  text primary key,
    route_id     text not null,
    from_vehicle text not null,
    to_vehicle   text not null,
    reason       text not null,
    created_at   text not null
);

create table if not exists manifest_pull (
    route_id   text not null,
    version_no integer not null,
    pulled_by  text not null,
    pulled_at  text not null
);

create table if not exists order_exception (
    exc_id     integer primary key autoincrement,
    order_id   text,
    route_id   text,
    store_id   text,
    type       text not null,
    product_id text,
    qty        real,
    detail     text not null,
    created_at text not null
);

create table if not exists event_log (
    event_id     text primary key,
    event_type   text not null,
    aggregate_id text not null,
    occurred_at  text not null,
    payload      text not null default '{}'
);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库并保证表结构存在。"""
    connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection
