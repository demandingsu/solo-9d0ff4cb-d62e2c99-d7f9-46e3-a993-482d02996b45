# 冷却水站班次水量平衡诊断

计量员每班登记各管段水表读数，接口自动核对全网水量是否账平；
账不平则指出“只错一只表”的嫌疑水表及该管段的可行水量范围，
并支持补登人工复测区间逐步定位失准表。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs 直接操作全部接口
```

本地开发（无需 Docker）：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000
```

## 规则与语义

- 每班一次登记至多 **20 个管段**，每段填：起点、终点、过水量上限、
  水表读数区间；水量一律为**非负整数升**。
- 除标记的**进出水口**外，每个节点流入量 = 流出量。
- 所有生效读数能同时成立 → **账平**，并给出一组可行流量。
- 否则逐一试算“仅忽略该表原读数”：能使全网账平的表列为**嫌疑表**，
  并给出该管段可行水量的**上下界**；一只都找不到则提示
  **单表误差无法解释**（可能多表失准或漏记管段）。
- **人工复测区间**：补登后**取代该表原读数**，且与过水量上限一样
  **始终有效**——即使诊断中忽略该表，复测区间也不被忽略；
  同一管段多次复测取交集。复测与由此产生的新诊断**同一事务生效**；
  计算或落库失败时不写库，原读数与诊断保持不变。
- 诊断与所用读数（含生效复测区间）存入 **SQLite**，按班次查询；
  重新登记读数不会清除历史诊断与该班已登记的复测区间。

## 多班交接的水箱存量核算

补水月报需要两只水箱在**各班交接时**的存量范围。把按交接顺序排列的
**2～6 个不同班次**的诊断号串成一条整段时间轴，并给两支水箱口及其容量，
即可求各时点两箱**各自及合计**在整段约束下的**可达上下界**：

- 每班沿用其诊断快照的**过水量上限**、**普通节点守恒**规则；
  各管段读数取**留档的复测交集**，没复测则用**原读数**。
- 每班每箱：**期末 − 期初 = 该口净流入**（流入该箱的管段流量之和
  减去流出该箱的流量之和）；相邻班次**共用交接存量**。
- 两箱须列在各班快照的**进出水口中**（因此不参与普通节点守恒，
  存量变化由上式单独刻画）。
- 所有时点的存量须在 **0 到容量**之间；可另给**任一时点**（含全段首尾）
  的**实测存量区间**，与上述约束取交集。
- 水量一律为**整数升**，用混合整数规划精确求解上下界。
- 结果连同所用**诊断号按账号保存**，可查询历次与最新一次；
  可**换诊断号整段重提**。
- **无解、写入失败**时本次退回，账号**在用结果保持不变**；
  核算期间同账号**已有另一份结果入库**时，本次同样退回（返回 409），
  保证每个账号的在用结果只有一份。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/accounts/{account}/reconciliations` | 提交核算并保存 |
| GET | `/accounts/{account}/reconciliations` | 该账号历次核算 |
| GET | `/accounts/{account}/reconciliations/latest` | 该账号在用（最新）核算 |

核算示例（诊断号 11、12 为按交接顺序的两班）：

```json
POST /accounts/核算员甲/reconciliations
{
  "diagnosis_ids": [11, 12],
  "tanks": [{"port": "TA", "capacity": 10},
            {"port": "TB", "capacity": 10}],
  "storage": [{"time_index": 0, "tank": "TA", "lo": 8, "hi": 8},
              {"time_index": 2, "tank": "TB", "lo": 4, "hi": 4}]
}
```

`storage` 可省略；`time_index` 为时点序号（0 为首班期初，n 为末班期末，
共 n+1 个时点）。返回每个时点 `bounds` 下 `TA`/`TB`/`total` 三组的
`min`/`max`，以及一组满足全部约束的 `witness`（可行流量与存量）。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/shifts/{shift}/readings` | 登记（替换）该班管段读数并立即诊断 |
| POST | `/shifts/{shift}/remeasurements` | 补登复测区间，与新诊断一起生效 |
| GET | `/shifts/{shift}/diagnoses` | 该班历次诊断（含所用读数） |
| GET | `/shifts/{shift}/diagnoses/latest` | 该班最新诊断 |
| GET | `/shifts/{shift}/readings` | 该班当前登记与生效复测区间 |
| GET | `/shifts` | 已有登记的班次 |
| POST | `/accounts/{account}/reconciliations` | 多班两箱存量核算并保存 |
| GET | `/accounts/{account}/reconciliations` | 按账号查询历次核算 |
| GET | `/accounts/{account}/reconciliations/latest` | 该账号在用核算 |

登记示例：

```json
POST /shifts/一班/readings
{
  "ports": ["IN", "OUT"],
  "segments": [
    {"key": "S1", "from_node": "IN", "to_node": "A",
     "capacity": 1000, "meter_lo": 100, "meter_hi": 200},
    {"key": "S2", "from_node": "A", "to_node": "B",
     "capacity": 1000, "meter_lo": 100, "meter_hi": 200},
    {"key": "S3", "from_node": "B", "to_node": "OUT",
     "capacity": 1000, "meter_lo": 300, "meter_hi": 400}
  ]
}
```

守恒要求 S1=S2=S3，S3 的 [300,400] 与其余两只矛盾，返回账不平、
嫌疑表 S3、S3 可行水量 [100,200]。补登复测
`{"remeasures": [{"segment_key": "S3", "rlo": 150, "rhi": 150}]}`
后，复测值取代 S3 原读数，全网账平，可行流量 150/150/150。

## 实现要点

- 诊断建模为混合整数规划（HiGHS，经 `scipy.optimize.milp`）：
  节点-弧关联矩阵 + 整数流量变量 + 各区间约束，精确求解。
- 核算把各班流量变量与各时点存量变量放进同一个 MILP：先确认整段可行，
  再对每个时点的每支水箱及合计分别求最小／最大值。
- 写路径均为“先算后写、单事务提交”：诊断在内存中算完，
  再把登记/复测与诊断结果放进同一 SQLite 事务；任一步失败整体回滚。
  核算落库用 `BEGIN IMMEDIATE` 取写锁后核对账号版本，实现同账号
  并发时“只有一份入库”，后到者退回。
- SQLite 路径由 `DATABASE_PATH` 指定（默认 `data/app.db`，
  Compose 中挂卷 `/data` 持久化）。

## 测试

```bash
python3 tests/smoke.py http://127.0.0.1:8000   # 需服务已启动
python3 -m pytest tests/test_reconcile.py -q   # 核算精确性/并发/失败保留
```

冒烟覆盖：跨节点矛盾定位单表、补登复测后账平、失败保留原诊断、
多表失准提示、复测协助定位、入参校验（>20 段/区间倒置/负数）、
按班次查询与持久化；以及两箱核算的基本界、首尾实测锁定、无解保留、
水箱口校验、同账号重提与历史查询。单元测试用穷举 oracle 交叉验证
上下界，并覆盖写入失败与同账号并发退回。
