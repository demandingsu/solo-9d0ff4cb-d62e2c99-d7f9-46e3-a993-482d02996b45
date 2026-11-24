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

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("全部冒烟用例通过")
