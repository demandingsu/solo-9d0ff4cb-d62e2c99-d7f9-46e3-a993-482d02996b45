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

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/shifts/{shift}/readings` | 登记（替换）该班管段读数并立即诊断 |
| POST | `/shifts/{shift}/remeasurements` | 补登复测区间，与新诊断一起生效 |
| GET | `/shifts/{shift}/diagnoses` | 该班历次诊断（含所用读数） |
| GET | `/shifts/{shift}/diagnoses/latest` | 该班最新诊断 |
| GET | `/shifts/{shift}/readings` | 该班当前登记与生效复测区间 |
| GET | `/shifts` | 已有登记的班次 |

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
- 写路径均为“先算后写、单事务提交”：诊断在内存中算完，
  再把登记/复测与诊断结果放进同一 SQLite 事务；任一步失败整体回滚。
- SQLite 路径由 `DATABASE_PATH` 指定（默认 `data/app.db`，
  Compose 中挂卷 `/data` 持久化）。

## 测试

```bash
python3 tests/smoke.py http://127.0.0.1:8000   # 需服务已启动
```

覆盖：跨节点矛盾定位单表、补登复测后账平、失败保留原诊断、
多表失准提示、复测协助定位、入参校验（>20 段/区间倒置/负数）、
按班次查询与持久化。
