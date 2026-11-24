#!/usr/bin/env python3
"""冒烟测试：对运行中的服务执行关键场景（仅用标准库）。

用法: python3 tests/smoke.py [base_url]   默认 http://127.0.0.1:8000
覆盖：跨节点矛盾 -> 单表嫌疑及可行界；补登复测 -> 账平；
      失败场景保留原诊断；多表失准 -> 单表无法解释；入参校验。
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
FAILED = []


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + urllib.parse.quote(path, safe="/"),
                                 data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  | {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


# 场景一（一班）：跨节点矛盾 —— 守恒要求 S1=S2=S3，S3 区间与其余两只矛盾
s1, r1 = call("POST", "/shifts/一班/readings", {
    "ports": ["IN", "OUT"],
    "segments": [
        {"key": "S1", "from_node": "IN", "to_node": "A",
         "capacity": 1000, "meter_lo": 100, "meter_hi": 200},
        {"key": "S2", "from_node": "A", "to_node": "B",
         "capacity": 1000, "meter_lo": 100, "meter_hi": 200},
        {"key": "S3", "from_node": "B", "to_node": "OUT",
         "capacity": 1000, "meter_lo": 300, "meter_hi": 400},
    ]})
check("一班登记返回 201", s1 == 201, f"got {s1}")
check("跨节点矛盾判为账不平", r1.get("balanced") is False, r1.get("message"))
sus = {s["segment_key"]: s for s in r1.get("suspects", [])}
check("唯一嫌疑表为 S3", set(sus) == {"S3"}, f"suspects={list(sus)}")
check("S3 可行水量界为 [100,200]",
      sus.get("S3", {}).get("feasible_min") == 100
      and sus.get("S3", {}).get("feasible_max") == 200,
      json.dumps(sus.get("S3")))

# 场景二（一班）：补登复测区间 S3∈[150,150]，与所有读数同时成立 -> 账平
s2, r2 = call("POST", "/shifts/一班/remeasurements",
              {"remeasures": [{"segment_key": "S3", "rlo": 150, "rhi": 150}]})
check("补登复测返回 201", s2 == 201, f"got {s2}")
check("复测后账平", r2.get("balanced") is True, r2.get("message"))
check("账平给出可行流量且 S3=150",
      (r2.get("assignment") or {}).get("S3") == 150,
      json.dumps(r2.get("assignment")))
check("诊断输入中记录了所用复测区间",
      r2.get("inputs", {}).get("remeasures", {}).get("S3") == {"rlo": 150, "rhi": 150})

# 场景三（一班）：失败保留 —— 对未登记管段补登复测，应 422 且原诊断不变
s3, r3 = call("POST", "/shifts/一班/remeasurements",
              {"remeasures": [{"segment_key": "S9", "rlo": 1, "rhi": 2}]})
check("复测未登记管段返回 422", s3 == 422, f"got {s3}: {r3.get('detail')}")
s3b, r3b = call("GET", "/shifts/一班/diagnoses/latest")
check("失败后最新诊断仍是复测后那次",
      s3b == 200 and r3b.get("id") == r2.get("diagnosis_id"),
      f"latest id={r3b.get('id')} expect {r2.get('diagnosis_id')}")
check("失败后诊断内容保持账平", r3b.get("result", {}).get("balanced") is True)

# 场景四（二班）：三只表两两矛盾 -> 单表误差无法解释
s4, r4 = call("POST", "/shifts/二班/readings", {
    "ports": ["IN", "OUT"],
    "segments": [
        {"key": "T1", "from_node": "IN", "to_node": "A",
         "capacity": 1000, "meter_lo": 0, "meter_hi": 10},
        {"key": "T2", "from_node": "A", "to_node": "B",
         "capacity": 1000, "meter_lo": 20, "meter_hi": 30},
        {"key": "T3", "from_node": "B", "to_node": "OUT",
         "capacity": 1000, "meter_lo": 40, "meter_hi": 50},
    ]})
check("二班账不平且无嫌疑表",
      r4.get("balanced") is False and r4.get("suspects") == [],
      json.dumps(r4.get("suspects")))
check("提示单表误差无法解释", "单表误差无法解释" in r4.get("message", ""),
      r4.get("message"))

# 场景四 b（二班）：对 T1 补登复测 [25,25]（取代原读数）-> T3 成为唯一嫌疑
s4b, r4b = call("POST", "/shifts/二班/remeasurements",
                {"remeasures": [{"segment_key": "T1", "rlo": 25, "rhi": 25}]})
sus4 = {s["segment_key"]: s for s in r4b.get("suspects", [])}
check("二班复测 T1 后唯一嫌疑为 T3", s4b == 201 and set(sus4) == {"T3"},
      f"got {s4b} suspects={list(sus4)}")
check("T3 可行水量界被复测锁定为 [25,25]",
      sus4.get("T3", {}).get("feasible_min") == 25
      and sus4.get("T3", {}).get("feasible_max") == 25,
      json.dumps(sus4.get("T3")))

# 场景五（三班）：直接账平
s5, r5 = call("POST", "/shifts/三班/readings", {
    "ports": ["IN", "OUT"],
    "segments": [
        {"key": "U1", "from_node": "IN", "to_node": "A",
         "capacity": 500, "meter_lo": 100, "meter_hi": 200},
        {"key": "U2", "from_node": "A", "to_node": "OUT",
         "capacity": 500, "meter_lo": 150, "meter_hi": 250},
    ]})
check("三班账平", r5.get("balanced") is True, r5.get("message"))
a5 = r5.get("assignment") or {}
check("可行流量满足守恒与区间",
      a5.get("U1") == a5.get("U2") and 150 <= a5.get("U1", 0) <= 200,
      json.dumps(a5))

# 场景六：入参校验 —— 21 个管段应 422
s6, _ = call("POST", "/shifts/四班/readings", {
    "ports": ["IN", "OUT"],
    "segments": [{"key": f"V{i}", "from_node": "IN", "to_node": "OUT",
                  "capacity": 10, "meter_lo": 0, "meter_hi": 10}
                 for i in range(21)]})
check("超过 20 个管段返回 422", s6 == 422, f"got {s6}")

# 场景七：按班次查询
s7, r7 = call("GET", "/shifts/一班/diagnoses")
check("一班共 2 次诊断（登记+复测）",
      s7 == 200 and len(r7.get("diagnoses", [])) == 2,
      f"count={len(r7.get('diagnoses', []))}")
s8, r8 = call("GET", "/shifts")
check("班次列表包含一/二/三班",
      s8 == 200 and {"一班", "二班", "三班"} <= set(r8.get("shifts", [])),
      json.dumps(r8.get("shifts"), ensure_ascii=False))
s9, r9 = call("GET", "/shifts/一班/readings")
eff = (r9.get("effective_remeasures") or {}).get("S3")
check("一班生效复测区间可查", s9 == 200 and eff == {"rlo": 150, "rhi": 150},
      json.dumps(eff))

# ---------------- 多班两箱存量核算 ----------------
# 场景八：两班（可扩到三班）共用同一进水口，TA/TB 为水箱口
PORTS = ["IN", "OUT", "TA", "TB"]
SEGS = [
    {"key": "F1", "from_node": "IN", "to_node": "J",
     "capacity": 100, "meter_lo": 0, "meter_hi": 10},
    {"key": "F2", "from_node": "J", "to_node": "TA",
     "capacity": 100, "meter_lo": 0, "meter_hi": 10},
    {"key": "F3", "from_node": "J", "to_node": "TB",
     "capacity": 100, "meter_lo": 0, "meter_hi": 10},
    {"key": "F4", "from_node": "TA", "to_node": "OUT",
     "capacity": 100, "meter_lo": 0, "meter_hi": 10},
    {"key": "F5", "from_node": "TB", "to_node": "OUT",
     "capacity": 100, "meter_lo": 0, "meter_hi": 10},
]
diag_ids = []
for name in ("交接一班", "交接二班", "交接三班"):
    s, r = call("POST", f"/shifts/{name}/readings",
                {"ports": PORTS, "segments": SEGS})
    check(f"{name}登记账平", s == 201 and r.get("balanced") is True,
          f"{s} {r.get('message')}")
    diag_ids.append(r["diagnosis_id"])

TANKS = [{"port": "TA", "capacity": 10}, {"port": "TB", "capacity": 10}]
s8, r8 = call("POST", "/accounts/核算员甲/reconciliations",
              {"diagnosis_ids": diag_ids, "tanks": TANKS})
check("核算返回 201 并带核算号", s8 == 201 and r8.get("reconciliation_id"),
      f"got {s8}: {r8.get('detail')}")
tp = {p["time_index"]: p["bounds"] for p in r8.get("result", {}).get("time_points", [])}
check("核算给出 n+1 个时点", set(tp) == {0, 1, 2, 3}, json.dumps(sorted(tp)))
check("无实测时各时点两箱可达 [0,10]、合计 [0,20]",
      all(tp[t]["TA"] == {"min": 0, "max": 10}
          and tp[t]["TB"] == {"min": 0, "max": 10}
          and tp[t]["total"] == {"min": 0, "max": 20} for t in tp),
      json.dumps(tp))
check("核算结果记录了所用诊断号",
      r8.get("diagnosis_ids") == diag_ids, json.dumps(r8.get("diagnosis_ids")))

# 场景九：全段首尾实测存量 -> 两端锁定；中间时点仍可达
s9, r9 = call("POST", "/accounts/核算员乙/reconciliations", {
    "diagnosis_ids": diag_ids[:2], "tanks": TANKS,
    "storage": [{"time_index": 0, "tank": "TA", "lo": 10, "hi": 10},
                {"time_index": 0, "tank": "TB", "lo": 0, "hi": 0},
                {"time_index": 2, "tank": "TA", "lo": 0, "hi": 0},
                {"time_index": 2, "tank": "TB", "lo": 10, "hi": 10}]})
tp9 = {p["time_index"]: p["bounds"] for p in r9.get("result", {}).get("time_points", [])}
check("首尾实测锁定两箱存量",
      s9 == 201 and tp9.get(0, {}).get("TA") == {"min": 10, "max": 10}
      and tp9.get(2, {}).get("TB") == {"min": 10, "max": 10},
      json.dumps(tp9))
check("实测时点回显所填区间",
      r9.get("result", {}).get("time_points", [{}])[0].get("measured", {}).get("TA")
      == {"lo": 10, "hi": 10})

# 场景十：整段无解 -> 422，且该账号没有入库
# 共用进水 F1<=10，两箱一班的合计净流入最多 10；要求首班从 0 升到 20 无解
s10, r10 = call("POST", "/accounts/核算员丙/reconciliations", {
    "diagnosis_ids": diag_ids[:2], "tanks": TANKS,
    "storage": [{"time_index": 0, "tank": "TA", "lo": 0, "hi": 0},
                {"time_index": 0, "tank": "TB", "lo": 0, "hi": 0},
                {"time_index": 1, "tank": "TA", "lo": 10, "hi": 10},
                {"time_index": 1, "tank": "TB", "lo": 10, "hi": 10}]})
check("整段无解返回 422", s10 == 422, f"got {s10}: {r10.get('detail')}")
s10b, _ = call("GET", "/accounts/核算员丙/reconciliations")
check("无解时该账号无入库记录", s10b == 404, f"got {s10b}")

# 场景十一：水箱口不在某班快照的进出水口 -> 422
s11, r11 = call("POST", "/accounts/核算员丁/reconciliations", {
    "diagnosis_ids": diag_ids[:2],
    "tanks": [{"port": "TA", "capacity": 10}, {"port": "ZZ不存在的口", "capacity": 10}]})
check("水箱口未列入快照进水口返回 422", s11 == 422,
      f"got {s11}: {r11.get('detail')}")

# 场景十二：换诊断号整段重提，按账号可查历史
s12, r12 = call("POST", "/accounts/核算员甲/reconciliations", {
    "diagnosis_ids": diag_ids[1:], "tanks": TANKS,
    "storage": [{"time_index": 0, "tank": "TB", "lo": 3, "hi": 3}]})
check("同账号换诊断号重提成功", s12 == 201, f"got {s12}: {r12.get('detail')}")
s12b, r12b = call("GET", "/accounts/核算员甲/reconciliations")
check("账号历次核算可查（2 次）",
      s12b == 200 and len(r12b.get("reconciliations", [])) == 2,
      f"count={len(r12b.get('reconciliations', []))}")
s12c, r12c = call("GET", "/accounts/核算员甲/reconciliations/latest")
check("最新核算为换号重提那次",
      s12c == 200 and r12c.get("diagnosis_ids") == diag_ids[1:],
      json.dumps(r12c.get("diagnosis_ids")))

# 场景十三：入参校验
s13, _ = call("POST", "/accounts/核算员戊/reconciliations",
              {"diagnosis_ids": [diag_ids[0]], "tanks": TANKS})
check("少于 2 个诊断号返回 422", s13 == 422, f"got {s13}")
s13b, _ = call("POST", "/accounts/核算员戊/reconciliations",
               {"diagnosis_ids": diag_ids * 3, "tanks": TANKS})
check("多于 6 个诊断号返回 422", s13b == 422, f"got {s13b}")
s13c, _ = call("POST", "/accounts/核算员戊/reconciliations",
               {"diagnosis_ids": [diag_ids[0], diag_ids[0]], "tanks": TANKS})
check("诊断号重复返回 422", s13c == 422, f"got {s13c}")
s13d, _ = call("POST", "/accounts/核算员戊/reconciliations",
               {"diagnosis_ids": diag_ids[:2],
                "tanks": [{"port": "TA", "capacity": 10}]})
check("水箱数不是 2 返回 422", s13d == 422, f"got {s13d}")

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("全部冒烟用例通过")
