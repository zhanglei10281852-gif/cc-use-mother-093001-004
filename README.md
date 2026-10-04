# 综合管廊空间预约与作业许可系统

面向城中村改造小型综合管廊（电力、通信、给水、消防同舱）的空间预约与作业许可系统。
解决多家施工方同一舱段、同一时间进场时，表格排期无法识别**净空、隔离距离、前后工序**冲突的问题。

## 能力总览

- **空间建模**：舱室横截面（净宽净高、既有设施分区及净空要求）、连续区段（里程链）、占位包络（里程×断面矩形×时间窗）、施工依赖（方案级 depends_on + 工序先后规则）
- **冲突计算**：提交方案即输出可解释冲突（区段/里程/时段/净距数值/对方许可），类型含 `OUT_OF_BOUNDS`（超断面限界）、`CLEARANCE`（既有设施净空不足）、`OVERLAP`（包络相交）、`SEPARATION`（专业隔离不足）、`PROCESS_ORDER`（工序倒置）、`NO_SECTION`（区段不连续）
- **审批流**：提交 → 技术复核（`tech_reviewer`）→ 运营批准（`ops_approver`，含有效期）→ 进场 → 撤场 → 验收（`acceptor`）→ 释放占位
- **紧急抢修**：按权限等级临时占用，自动暂停受影响许可并生成恢复顺序；抢修验收后按顺序自动恢复
- **版本与幂等**：方案变更基于版本提交，旧版本审批即时作废；过期审批不能进场；幂等键重复请求返回原结果；撤场验收完成前占位不释放（已进场许可占位延展至验收）
- **审计**：全部状态变更落审计日志，SHA-256 哈希链防篡改，可校验完整性

## 目录结构

```
src/tunnel_booking/
  contracts.py   区段/横截面/占位包络基础契约
  geometry.py    里程区间与断面矩形计算
  rules.py       隔离距离表、工序先后规则（可按规范调整）
  conflicts.py   冲突检测引擎（可解释原因）
  models.py      占位需求、状态常量、时间工具
  store.py       SQLite 持久化 + 审计哈希链 + 幂等键
  service.py     业务编排（提交/审批/进场/抢修/恢复/验收）
  api.py         REST 接口（标准库 http.server）
  cli.py         命令行
  demo.py        端到端演示场景
```

## 运行

```bash
# 测试（35 个用例）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tests run_cli.py

# 端到端冒烟演示（冲突解释、抢修暂停、恢复顺序、审计验证）
python3 run_cli.py
```

## 命令行

```bash
export PYTHONPATH=src
DB="--db gallery.db"

# 登记连续区段（含横截面与既有设施分区）
python3 -m tunnel_booking.cli $DB add-section --file section.json

# 提交方案（计算冲突；幂等键防重复提交）
python3 -m tunnel_booking.cli $DB submit --file plan.json --idempotency-key K1

# 技术复核 / 运营批准（角色通过 --roles 传入）
python3 -m tunnel_booking.cli $DB --roles tech_reviewer tech-review --plan PL-0001 --version 1
python3 -m tunnel_booking.cli $DB --roles ops_approver ops-approve --plan PL-0001 --version 1 --valid-hours 24

# 进场 / 撤场 / 验收
python3 -m tunnel_booking.cli $DB check-in --permit PM-0001
python3 -m tunnel_booking.cli $DB acceptance-request --permit PM-0001
python3 -m tunnel_booking.cli $DB --roles acceptor acceptance --permit PM-0001

# 方案变更（生成新版本，旧审批作废）
python3 -m tunnel_booking.cli $DB amend --plan PL-0001 --file plan_v2.json

# 紧急抢修（按权限等级；自动暂停并生成恢复顺序）
python3 -m tunnel_booking.cli $DB --emergency-level 2 emergency --file emergency.json
python3 -m tunnel_booking.cli $DB recovery-plan --permit PM-0002

# 查询：区段占用 / 冲突重算 / 审计
python3 -m tunnel_booking.cli $DB occupancy --section SEC-A
python3 -m tunnel_booking.cli $DB conflicts --plan PL-0001
python3 -m tunnel_booking.cli $DB audit --entity-id PM-0001
python3 -m tunnel_booking.cli $DB audit-verify

# 启动 REST 服务
python3 -m tunnel_booking.cli $DB serve --port 8080
```

## REST 接口

身份经请求头传递：`X-Actor-Name`、`X-Actor-Roles`、`X-Actor-Emergency-Level`
（头部仅支持 latin-1，中文名用 percent-encoding）；幂等经 `Idempotency-Key` 头。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/sections` | 登记/更新区段 |
| GET | `/sections/{id}/occupancy` | 区段当前占位 |
| POST | `/plans` | 提交方案（返回冲突报告） |
| POST | `/plans/{id}/amend` | 版本化变更 |
| GET | `/plans/{id}/conflicts` | 重算冲突 |
| POST | `/plans/{id}/tech-review` | 技术复核 |
| POST | `/plans/{id}/ops-approve` | 运营批准（生成许可） |
| POST | `/permits/{id}/check-in` | 进场（校验审批未过期） |
| POST | `/permits/{id}/acceptance-request` | 撤场申请 |
| POST | `/permits/{id}/acceptance` | 验收（通过才释放占位） |
| POST | `/emergency/occupy` | 抢修临时占用（自动暂停+恢复顺序） |
| GET | `/permits/{id}/recovery-plan` | 恢复顺序 |
| GET | `/audit`、`/audit/verify` | 审计查询 / 哈希链校验 |

`tests/test_api.py` 通过接口完整验证了跨区段预约、紧急抢修、许可恢复与审计记录。

## 方案 JSON 示例

```json
{
  "depends_on": ["PL-0001"],
  "occupancies": [{
    "request_id": "RQ-1",
    "chainage_start_m": 150, "chainage_end_m": 260,
    "rect": {"x_m": 1.0, "y_m": 0.0, "width_m": 1.0, "height_m": 1.8},
    "utility": "POWER", "process": "CABLE_LAYING",
    "time_start": "2026-10-05T08:00:00+00:00",
    "time_end": "2026-10-05T16:00:00+00:00"
  }]
}
```

占位里程可跨多个连续区段（系统自动切分并逐段校验）；`utility` ∈ POWER/TELECOM/WATER/FIRE/GENERAL，
`process` ∈ STRUCTURE_REPAIR/PIPE_LAYING/CABLE_LAYING/PRESSURE_TEST/RESTORE/MAINTENANCE/EMERGENCY/GENERAL。

## 关键规则（`rules.py` 可配）

- 电力↔通信/给水/消防最小隔离 0.5m，其余专业组合 0.3m，同专业按区段默认隔离
- 既有设施分区各自声明净空要求（如既有电力电缆 0.5m）
- 工序：结构修补 → 管线/线缆敷设 → 压力试验 → 回填恢复
- 许可状态机：APPROVED → ACTIVE →（SUSPENDED ↔ 恢复）→ ACCEPTANCE_PENDING → COMPLETED；验收前占位不释放
