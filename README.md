# 门店配送波次中枢

门店需求、车辆、波次、周转箱与扫描事件的**波次编排与交接系统**：
以只追加事件为事实来源（event sourcing + SQLite），把"领单 → 装箱 → 封签 →
签收 → 回收"绑定到同一配送版本，终止"箱子跑错线路、改单时点说不清、
并发封签结果不一致、故障转派后容器失联"这类混乱。

## 领域资料

- `domain/contract.v2.json`：最新版合同——实体、状态、事件类型、原因码与九条业务规则。
- `domain/contract.json`：v1 初始合同（保留，不删除）。新版本只追加，不覆盖旧声明。
- `examples/events.json`：按业务发生时间排列的样例事件（含转派、回收）。
- `tools/validate_contract.py`：自动选取最高版本合同做一致性校验。

## 系统（`wavehub/`，仅依赖 Python 3.11 标准库）

| 模块 | 职责 |
| --- | --- |
| `models.py` | 门店需求、车辆（载重/温层/线路时长）、回收任务输入模型 |
| `planner.py` | 五约束编排：收货窗口、载重（含回收皮重）、温层、共载限制、线路时长；无解则拆波/拆单并记录原因码 |
| `store.py` | SQLite 事件存储；`scan_dedup` 扫描幂等、`seal_record` 封签唯一约束（并发只留一个结果） |
| `projection.py` | 事件重放成当前状态；容器逐件交接链；崩溃后重放即恢复 |
| `app.py` | 全部业务用例与对外查询（`WaveHub`） |
| `errors.py` | 稳定原因码（缺货/拆单/延迟/交接/转派） |

### 关键规则落点

- **波次由约束决定**：窗口、载重、温层、共载、线路时长、回收任务任一违反即不编排；
  超载重按 `CAPACITY_LIMIT` 拆单，共载冲突按 `COLOADING_CONFLICT` 拆波，均留原因。
- **截单 / 差异单**：截单前可合并需求；截单后只有"已批准差异单"能改，且只能改**未封签**部分；
  改单业务时间晚于封签时间一律拒绝（`ADJUSTMENT_TARGET_SEALED`）。
- **同一配送版本**：领单、装箱、封签、签收都校验 `delivery_version`；差异单/转派升版本，旧领单失效需重领。
- **离线扫描**：以 `occurred_at`（业务时间）校验，`recorded_at` 仅记录上传；
  跨线路箱在装车口即拒（`CONTAINER_ROUTE_MISMATCH`）；重复扫描不二次装车/签收（`DUPLICATE_SCAN`）。
- **并发封签**：数据库唯一约束兜底，只留一个结果（`CONCURRENT_SEAL`）。
- **故障转派**：整车转派并重校全部约束；仍在车上的容器逐件转移留痕，已交接给门店的容器不动但全程可追。
- **门店透明**：`store_status()` 给出缺货（`SUPPLY_SHORT`/`NOT_LOADED`/`REMOVED_BY_ADJUSTMENT`）、
  拆单、延迟（`VEHICLE_BREAKDOWN`/`REASSIGNMENT`/`WAVE_REPLAN`）的具体原因与波次/容器。
- **回看时间线**：`wave_timeline()` 按业务时间列出节点，改单在封签前还是后一目了然。
- **崩溃恢复**：`WaveHub(db_path)` 启动即重放；`pending_recovery()` 巡检待转派车辆与未回收容器。

## 快速体验

```bash
python3 tools/demo.py            # 十个场景的端到端演示（内存运行）
```

## 构建

```bash
python3 -m compileall -q .
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 资料校验

```bash
python3 tools/validate_contract.py
```

所有命令均在项目根目录执行，不需要启动额外服务。
