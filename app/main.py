"""冷却水站班次水量平衡诊断接口。

接口一览（均可在 /docs 中直接操作）：
  POST /shifts/{shift}/readings        登记一班管段读数并立即诊断（至多 20 段）
  POST /shifts/{shift}/remeasurements  补登人工复测区间，与新诊断一起生效
  GET  /shifts/{shift}/diagnoses       按班次查询历次诊断（含所用读数）
  GET  /shifts/{shift}/diagnoses/latest  查询该班最新诊断
  GET  /shifts/{shift}/readings        查询该班当前登记与生效的复测区间
  GET  /shifts                         列出已有登记的班次
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from . import db
from .schemas import ReadingsIn, RemeasureBatch
from .solver import Segment, SolverError, diagnose


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="冷却水站班次水量平衡诊断",
    version="1.0.0",
    description="登记每班至多 20 个管段的起止节点、过水量上限与水表读数区间；"
                "除进出水口外各节点进出水量相等。账平则给出可行流量；"
                "账不平则列出“仅忽略该表原读数即可账平”的水表及其管段可行水量上下界；"
                "支持补登人工复测区间——复测值取代该表原读数，"
                "与过水量上限一样始终有效。",
    lifespan=lifespan,
)


# ---------------- 内部工具 ----------------

def _effective_remeasures(shift: str) -> dict[str, tuple[int, int]]:
    """该班全部复测区间：同一管段多次复测取交集（所有复测始终有效）。"""
    merged: dict[str, tuple[int, int]] = {}
    for r in db.remeasurements_for_shift(shift):
        lo, hi = merged.get(r["segment_key"], (0, 1 << 62))
        merged[r["segment_key"]] = (max(lo, r["rlo"]), min(hi, r["rhi"]))
    return merged


def _run_diagnosis(registration: dict,
                   remeasures: dict[str, tuple[int, int]]) -> tuple[dict, dict]:
    """组装输入、执行诊断，返回 (inputs, result)。失败抛异常，不写库。"""
    segments = [Segment(key=s["segment_key"], src=s["src"], dst=s["dst"],
                        capacity=s["capacity"], meter_lo=s["meter_lo"],
                        meter_hi=s["meter_hi"])
                for s in registration["segments"]]
    # 复测区间只作用于当前登记中存在的管段
    active_rem = {k: v for k, v in remeasures.items()
                  if k in {s.key for s in segments}}
    outcome = diagnose(segments, set(registration["ports"]), active_rem)

    inputs = {
        "ports": registration["ports"],
        "segments": [{"key": s.key, "from_node": s.src, "to_node": s.dst,
                      "capacity": s.capacity, "meter_lo": s.meter_lo,
                      "meter_hi": s.meter_hi} for s in segments],
        "remeasures": {k: {"rlo": lo, "rhi": hi}
                       for k, (lo, hi) in sorted(active_rem.items())},
    }
    if outcome["balanced"]:
        result = {"balanced": True,
                  "message": "账平：所有生效读数（含复测区间）可同时成立",
                  "assignment": outcome["assignment"],
                  "suspects": []}
    elif outcome["suspects"]:
        result = {"balanced": False,
                  "message": "账不平：仅忽略下列任一只表的原读数即可使全网账平",
                  "assignment": None,
                  "suspects": outcome["suspects"]}
    else:
        result = {"balanced": False,
                  "message": "账不平：单表误差无法解释，"
                             "可能存在多只表同时失准或漏记管段",
                  "assignment": None,
                  "suspects": []}
    return inputs, result


def _diagnosis_response(shift: str, diagnosis_id: int, inputs: dict,
                        result: dict) -> dict:
    return {"diagnosis_id": diagnosis_id, "shift": shift,
            "inputs": inputs, **result}


# ---------------- 写接口 ----------------

@app.post("/shifts/{shift}/readings", status_code=201,
          summary="登记一班管段读数并诊断")
def post_readings(shift: str, body: ReadingsIn):
    """登记（替换）该班管段读数，立即诊断并落库。

    该班此前补登的复测区间仍然有效并参与本次诊断。
    诊断或落库失败时，原有读数与诊断保持不变。
    """
    registration = {"ports": body.ports,
                    "segments": [{"segment_key": s.key, "src": s.from_node,
                                  "dst": s.to_node, "capacity": s.capacity,
                                  "meter_lo": s.meter_lo,
                                  "meter_hi": s.meter_hi}
                                 for s in body.segments]}
    remeasures = _effective_remeasures(shift)
    try:
        inputs, result = _run_diagnosis(registration, remeasures)
    except SolverError as exc:
        raise HTTPException(status_code=500,
                            detail=f"诊断计算失败，已保留原读数与诊断：{exc}")
    try:
        diag_id = db.save_registration_with_diagnosis(
            shift, body.ports,
            [s.model_dump() for s in body.segments], inputs, result)
    except Exception as exc:  # 落库失败：事务回滚，旧数据不变
        raise HTTPException(status_code=500,
                            detail=f"保存失败，已保留原读数与诊断：{exc}")
    return _diagnosis_response(shift, diag_id, inputs, result)


@app.post("/shifts/{shift}/remeasurements", status_code=201,
          summary="补登人工复测区间并重新诊断")
def post_remeasures(shift: str, body: RemeasureBatch):
    """补登复测区间，与由此产生的新诊断同一事务生效。

    复测值取代该表原读数，且与过水量上限一样始终有效
    （即使诊断中忽略该表，复测区间也不被忽略）；
    同一管段多次复测取交集。任何一步失败都不写库，原读数与诊断保持不变。
    """
    registration = db.latest_registration(shift)
    if registration is None:
        raise HTTPException(status_code=404,
                            detail=f"班次 {shift} 尚未登记读数，无法补登复测")
    known = {s["segment_key"] for s in registration["segments"]}
    unknown = [r.segment_key for r in body.remeasures
               if r.segment_key not in known]
    if unknown:
        raise HTTPException(status_code=422,
                            detail=f"复测对象不在当前登记中：{unknown}")

    remeasures = _effective_remeasures(shift)
    for r in body.remeasures:  # 与历史复测取交集
        lo, hi = remeasures.get(r.segment_key, (0, 1 << 62))
        remeasures[r.segment_key] = (max(lo, r.rlo), min(hi, r.rhi))
    try:
        inputs, result = _run_diagnosis(registration, remeasures)
    except SolverError as exc:
        raise HTTPException(status_code=500,
                            detail=f"诊断计算失败，已保留原读数与诊断：{exc}")
    try:
        diag_id = db.save_remeasures_with_diagnosis(
            shift, [r.model_dump() for r in body.remeasures],
            registration["id"], inputs, result)
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"保存失败，已保留原读数与诊断：{exc}")
    return _diagnosis_response(shift, diag_id, inputs, result)


# ---------------- 查询接口 ----------------

@app.get("/shifts", summary="列出已有登记的班次")
def get_shifts():
    return {"shifts": db.list_shifts()}


@app.get("/shifts/{shift}/diagnoses", summary="按班次查询历次诊断")
def get_diagnoses(shift: str):
    rows = db.diagnoses_for_shift(shift)
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"班次 {shift} 没有任何诊断记录")
    return {"shift": shift, "diagnoses": rows}


@app.get("/shifts/{shift}/diagnoses/latest", summary="查询该班最新诊断")
def get_latest_diagnosis(shift: str):
    row = db.latest_diagnosis(shift)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"班次 {shift} 没有任何诊断记录")
    return row


@app.get("/shifts/{shift}/readings", summary="查询该班当前登记与生效复测区间")
def get_readings(shift: str):
    registration = db.latest_registration(shift)
    if registration is None:
        raise HTTPException(status_code=404,
                            detail=f"班次 {shift} 尚未登记读数")
    return {"shift": shift, "registration": registration,
            "remeasures": db.remeasurements_for_shift(shift),
            "effective_remeasures": {k: {"rlo": lo, "rhi": hi} for k, (lo, hi)
                                     in _effective_remeasures(shift).items()}}


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}
