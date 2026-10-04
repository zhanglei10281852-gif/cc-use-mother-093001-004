# 综合管廊空间预约与作业许可系统

面向城中村改造小型综合管廊（电力 / 通信 / 给水 / 消防共舱）的**空间预约 + 作业许可**系统。
解决多家施工方在同一舱段、同一时间进场时，表格排期无法识别的三类问题：

1. **净空冲突**——横截面占位重叠、超出舱室边界、压占永久设施或安全检修通道；
2. **隔离距离冲突**——异专业管线之间净距不满足规范（电力/通信 0.30m、电力/消防 0.50m 等，矩阵可配）；
3. **前后工序冲突**——前置工序未完工即开工、依赖缺失、依赖成环。

仅依赖 Python 3.8+ 标准库，无第三方包。

## 领域模型

| 概念 | 说明 |
|---|---|
| `Compartment` | 舱室横截面：内净空（宽×高）、永久设施占位 `fixtures`、必须畅通的安全通道 `safety_passage` |
| `Segment` | 舱室内的连续区段（里程区间，如 S1: 0–120m、S2: 120–240m） |
| `WorkEnvelope` | 占位包络：某舱室某区段某段里程上的横截面矩形 + 所属专业；一个申请可携带多个包络，天然支持**跨区段预约** |
| `TimeWindow` | 作业时间窗（半开区间，端点相接不算重叠） |
| `Proposal` | 一次方案提交：施工方、时间窗、多个包络、前置工序 `depends_on`、恢复优先级 |

冲突判定三重门控：**舱室相同 + 里程重叠 + 时间重叠**，才比较横截面。

## 许可流程

```
提交方案 ──有冲突──▶ REJECTED（附可解释原因，修改后提交新版本）
   │无冲突
   ▼
SUBMITTED ──技术复核──▶ TECH_APPROVED ──运营批准(带有效期)──▶ APPROVED ──进场──▶ ACTIVE
                                                                          │
紧急抢修占空间 ─────────────────────────────▶ ACTIVE 自动变 SUSPENDED ◀────┘
                                                          │ 抢修结束按恢复顺序逐个恢复
                                                          ▼
                                                       ACTIVE ──申请撤场──▶ INSPECTION
                                                                          │
                                              验收不通过退回 ACTIVE（占位不释放）
                                              验收通过 ──▶ COMPLETED（释放占位）
```

关键规则：

- **两级审批**：技术复核 → 运营批准；运营批准带有效期（默认 24h），**过期审批不能进场**，状态置 `EXPIRED`。
- **方案版本化**：变更必须显式 `base_version=当前版本` 提交；基于过期版本提交被拒；在场版本须先撤场才能变更。
- **幂等**：同一 `request_id` + 完全相同的方案体重提，返回原版本结果（`idempotent: true`），不产生新版本。
- **紧急抢修**：需 `authorized` 权限（无权限返回 403）；拓扑净空硬约束仍校验；与在位作业的空间冲突不阻塞，而是**自动暂停所有受影响许可**，并生成**恢复顺序**（先按工序依赖拓扑序，再按专业优先级：消防→给水→电力→通信）。
- **撤场验收**：进场后不得直接撤回（返回 423 `OCCUPANCY_LOCKED`）；验收中仍占位；验收不合格退回作业且占位不释放；**验收通过才释放占位**。
- **完整审计**：所有动作只追加、连续编号，可按实体 / 动作 / 操作人过滤追溯。

## 运行

```bash
# 端到端验证场景（41 项断言 + 完整审计输出）
python run_cli.py demo

# 启动 HTTP 接口
python run_cli.py serve --host 127.0.0.1 --port 8080

# 打印演示管廊拓扑
python run_cli.py topology

# 测试
python -m unittest discover -s tests -v
python -m compileall -q src tests run_cli.py
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` `/topology` | 健康检查、管廊拓扑 |
| POST | `/requests/submit` | 提交/变更方案（body 为 proposal JSON） |
| GET | `/requests` `/requests/{id}` | 列表 / 详情 |
| POST | `/requests/{id}/technical-review` | 技术复核 `{"approved":bool,"comment":...}` |
| POST | `/requests/{id}/operational-approval` | 运营批准（返回 `expires_at`） |
| POST | `/requests/{id}/activate` `/withdraw` | 进场 / 进场前撤回 |
| POST | `/requests/{id}/inspection/request` | 申请撤场验收 |
| POST | `/requests/{id}/inspection/complete` | 验收 `{"accepted":bool}` |
| POST | `/emergencies` | 紧急抢修占用 `{"emergency_id","authorized","proposal":...}` |
| POST | `/emergencies/{id}/recover` `/close` | 按序恢复下一许可 / 关闭抢修 |
| GET | `/emergencies/{id}` | 抢修与恢复进度 |
| GET | `/audit?entity_id=&action=&actor=` | 审计查询 |

操作人通过请求头 `X-Actor` 传递。错误以 HTTP 状态码区分：400 校验、403 无权限、404 不存在、409 状态冲突、410 审批过期、423 占位锁定。

### curl 示例

```bash
curl -s -X POST http://127.0.0.1:8080/requests/submit \
  -H 'X-Actor: contractor-li' -H 'Content-Type: application/json' -d '{
  "request_id":"P-1","contractor":"送变电三队",
  "window":{"start":"2026-10-05T08:00","end":"2026-10-05T18:00"},
  "envelopes":[{"envelope_id":"E1","compartment_id":"C1","segment_id":"S1",
    "chainage":{"start_m":20,"end_m":100},
    "footprint":{"x_m":1.0,"y_m":1.0,"width_m":0.4,"height_m":0.4},
    "utility":"power"}],
  "depends_on":[]}'
```

冲突返回示例（可解释原因 + 结构化定位）：

```json
{
  "code": "SEPARATION_VIOLATION",
  "category": "SPACE",
  "message": "申请 C-1（通信）与申请 P-1（电力）在舱室 C1 里程 60~100m 的实际净距 0.10m 小于规范要求的 0.30m，隔离距离不足，且作业时间重叠",
  "details": {
    "compartment_id": "C1",
    "chainage_overlap_m": [60, 100],
    "time_overlap": ["2026-10-05T10:00:00", "2026-10-05T16:00:00"],
    "required_separation_m": 0.3,
    "actual_gap_m": 0.1
  }
}
```

## 代码结构

```
src/tunnel_booking/
  contracts.py     # 舱室/区段/包络/时间窗/方案 契约
  topology.py      # 拓扑、净空校验、专业间隔离距离矩阵
  conflicts.py     # 空间/隔离/时间门控/工序依赖/环 冲突引擎
  models.py        # 许可聚合、版本、审批、状态机、抢修记录
  service.py       # 核心服务：提交/版本/幂等/审批/抢修/恢复/撤场/审计
  audit.py         # 只追加审计日志
  serialization.py # JSON 序列化
  api.py           # 标准库 HTTP 接口（ThreadingHTTPServer）
  cli.py           # 端到端验证场景（demo）
  demo.py          # 演示拓扑（1 舱 2 区段 + 设施 + 安全通道）
tests/             # 48 个 unittest：契约/冲突引擎/服务流程/HTTP 端到端
```

## 隔离距离矩阵（`topology.DEFAULT_SEPARATION_M`，可覆盖）

| | 电力 | 通信 | 给水 | 消防 |
|---|---|---|---|---|
| 电力 | — | 0.30 | 0.30 | 0.50 |
| 通信 | 0.30 | — | 0.15 | 0.30 |
| 给水 | 0.30 | 0.15 | — | 0.50 |
| 消防 | 0.50 | 0.30 | 0.50 | — |
