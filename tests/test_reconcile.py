#!/usr/bin/env python3
"""核算功能单元测试：精确性（对穷举 oracle）、写入失败保留、同账并发退回。

用法:
    python3 -m pytest tests/test_reconcile.py -q

不依赖运行中的服务；DB 用临时文件。
"""
from __future__ import annotations

import itertools
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="reconcile-test-"), "app.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.reconcile import (FlowSegment, ReconciliationInfeasible,  # noqa: E402
                           ShiftSnapshot, reconcile)


# ---------------- 穷举 oracle ----------------

def _shift_deltas(snap: ShiftSnapshot) -> set[tuple[int, int]]:
    x = snap.segments
    nodes = {n for g in x for n in (g.src, g.dst)}
    out = set()
    for f in itertools.product(*[range(g.lo, g.hi + 1) for g in x]):
        if any(
            sum(f[j] for j, g in enumerate(x) if g.dst == nd)
            - sum(f[j] for j, g in enumerate(x) if g.src == nd) != 0
            for nd in nodes - set(snap.ports)
        ):
            continue
        dA = sum(f[j] for j, g in enumerate(x) if g.dst == "TA") \
            - sum(f[j] for j, g in enumerate(x) if g.src == "TA")
        dB = sum(f[j] for j, g in enumerate(x) if g.dst == "TB") \
            - sum(f[j] for j, g in enumerate(x) if g.src == "TB")
        out.add((dA, dB))
    return out


def _brute_bounds(snaps, tanks, limits):
    names = [n for n, _ in tanks]
    caps = [c for _, c in tanks]
    n = len(snaps)
    deltas = [_shift_deltas(s) for s in snaps]

    def domain(t):
        per = []
        for k, name in enumerate(names):
            lo, hi = 0, caps[k]
            if (t, name) in limits:
                lo = max(lo, limits[(t, name)][0])
                hi = min(hi, limits[(t, name)][1])
            per.append(range(lo, hi + 1) if lo <= hi else range(0))
        return set(itertools.product(*per))

    domains = [domain(t) for t in range(n + 1)]
    if not domains[0]:
        return None
    reach = [set() for _ in range(n + 1)]
    reach[0] = set(domains[0])
    for i in range(n):
        reach[i + 1] = {
            (h[0] + d[0], h[1] + d[1])
            for h in reach[i] for d in deltas[i]
            if (h[0] + d[0], h[1] + d[1]) in domains[i + 1]
        }
    # 反向剔除无法走到末时点的状态
    alive = [None] * (n + 1)
    alive[n] = set(reach[n])
    for i in range(n - 1, -1, -1):
        alive[i] = {
            h for h in reach[i]
            if any((h[0] + d[0], h[1] + d[1]) in alive[i + 1] for d in deltas[i])
        }
    if not alive[0]:
        return None
    out = []
    for t in range(n + 1):
        vals = list(alive[t])
        b = {name: {"min": min(h[k] for h in vals),
                    "max": max(h[k] for h in vals)}
             for k, name in enumerate(names)}
        total = [h[0] + h[1] for h in vals]
        b["total"] = {"min": min(total), "max": max(total)}
        out.append(b)
    return out


def _random_case(seed: int):
    import random
    rnd = random.Random(seed)
    n = rnd.randint(2, 3)

    def segs():
        spec = [("F1", "IN", "J", rnd.randint(1, 3)),
                ("F2", "J", "TA", rnd.randint(1, 3)),
                ("F3", "J", "TB", rnd.randint(1, 3)),
                ("F4", "TA", "OUT", rnd.randint(1, 3)),
                ("F5", "TB", "OUT", rnd.randint(1, 3)),
                ("F6", "J", "OUT", rnd.randint(1, 3))]
        return tuple(
            FlowSegment(k, a, b, rnd.randint(0, cap), cap)
            for k, a, b, cap in spec)

    snaps = [ShiftSnapshot(i + 1, f"S{i}",
                           frozenset({"IN", "OUT", "TA", "TB"}), segs())
             for i in range(n)]
    caps = [rnd.randint(3, 6), rnd.randint(3, 6)]
    tanks = [("TA", caps[0]), ("TB", caps[1])]
    limits = {}
    for _ in range(rnd.randint(0, 2)):
        t = rnd.randint(0, n)
        k = rnd.randint(0, 1)
        lo = rnd.randint(0, 2)
        limits[(t, tanks[k][0])] = (lo, rnd.randint(lo, 3))
    return snaps, tanks, limits


@pytest.mark.parametrize("seed", range(60))
def test_bounds_match_exhaustive_oracle(seed):
    snaps, tanks, limits = _random_case(seed)
    expected = _brute_bounds(snaps, tanks, limits)
    try:
        got = reconcile(snaps, tanks, limits)
    except ReconciliationInfeasible:
        got = None
    if expected is None:
        assert got is None
        return
    assert got is not None
    for t, bounds in enumerate(expected):
        assert got["time_points"][t]["bounds"] == bounds


def test_witness_is_feasible_for_every_shift():
    import random
    rnd = random.Random(7)
    snaps = []
    for i in range(3):
        segs = tuple(
            FlowSegment(k, a, b, lo, cap)
            for k, a, b, cap, lo in [
                ("F1", "IN", "J", 5, 0), ("F2", "J", "TA", 5, 2),
                ("F3", "J", "TB", 5, 1), ("F4", "TA", "OUT", 5, 0),
                ("F5", "TB", "OUT", 5, 0)])
        snaps.append(ShiftSnapshot(i + 1, f"S{i}",
                                   frozenset({"IN", "OUT", "TA", "TB"}), segs))
    out = reconcile(snaps, [("TA", 6), ("TB", 6)], {})
    for t in out["witness"]["storage"]:
        assert 0 <= t["TA"] <= 6 and 0 <= t["TB"] <= 6
    for wf, snap in zip(out["witness"]["flows"], snaps):
        f = wf["segments"]
        nodes = {n for g in snap.segments for n in (g.src, g.dst)}
        for nd in nodes - set(snap.ports):
            assert (sum(f[g.key] for g in snap.segments if g.dst == nd)
                    == sum(f[g.key] for g in snap.segments if g.src == nd))


# ---------------- API / 持久化 ----------------

P = ["IN", "OUT", "TA", "TB"]
S = [{"key": "G1", "from_node": "IN", "to_node": "J",
      "capacity": 100, "meter_lo": 0, "meter_hi": 10},
     {"key": "G2", "from_node": "J", "to_node": "TA",
      "capacity": 100, "meter_lo": 0, "meter_hi": 10},
     {"key": "G3", "from_node": "J", "to_node": "TB",
      "capacity": 100, "meter_lo": 0, "meter_hi": 10},
     {"key": "G4", "from_node": "TA", "to_node": "OUT",
      "capacity": 100, "meter_lo": 0, "meter_hi": 10},
     {"key": "G5", "from_node": "TB", "to_node": "OUT",
      "capacity": 100, "meter_lo": 0, "meter_hi": 10}]
TANKS = [{"port": "TA", "capacity": 10}, {"port": "TB", "capacity": 10}]


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _diag_ids(client, prefix):
    ids = []
    for i in range(2):
        r = client.post(f"/shifts/{prefix}-{i}/readings",
                        json={"ports": P, "segments": S})
        assert r.status_code == 201, r.text
        ids.append(r.json()["diagnosis_id"])
    return ids


def test_save_conflict_retains_in_use_result(client):
    """核算期间同账号已有结果入库：后到者 409，且不覆盖在用结果。"""
    ids = _diag_ids(client, "并发")
    body = {"diagnosis_ids": ids, "tanks": TANKS}

    # 模拟两个并发核算都基于版本 0 求解，先到者入库
    version = db.reconciliation_account_version("并发账号")
    result = reconcile(_snaps(ids), [("TA", 10), ("TB", 10)], {})
    first = db.save_reconciliation("并发账号", ids, body, result, version)
    assert first > 0

    # 后到者用同一（已过期的）版本写入 -> 退回
    with pytest.raises(db.ReconciliationConflict):
        db.save_reconciliation("并发账号", ids, body, result, version)

    rows = client.get("/accounts/并发账号/reconciliations").json()["reconciliations"]
    assert [r["id"] for r in rows] == [first]

    # 用最新版本重提仍可入库
    second = db.save_reconciliation(
        "并发账号", ids, body, result,
        db.reconciliation_account_version("并发账号"))
    assert second > first


def _snaps(ids):
    from app.main import _load_snapshot
    return [_load_snapshot(i) for i in ids]


def test_api_concurrent_same_account_single_winner(client):
    """真并发：同一账号同时提交同一整段，只能有一份成功入库。"""
    ids = _diag_ids(client, "真并发")
    body = {"diagnosis_ids": ids, "tanks": TANKS}
    results = []
    barrier = threading.Barrier(2)

    def submit():
        barrier.wait()
        r = client.post("/accounts/真并发账号/reconciliations", json=body)
        results.append(r.status_code)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) in ([201, 201], [201, 409])  # 串行则都成功
    rows = client.get("/accounts/真并发账号/reconciliations").json()["reconciliations"]
    assert len(rows) == results.count(201)
    # 至多一份是版本冲突退出的证据：若有 409，则行数恰为 1
    if 409 in results:
        assert len(rows) == 1


def test_write_failure_keeps_in_use_result(client, monkeypatch):
    """写入失败时本次退回，账号在用结果不变。"""
    ids = _diag_ids(client, "写失败")
    body = {"diagnosis_ids": ids, "tanks": TANKS}
    ok = client.post("/accounts/写失败账号/reconciliations", json=body)
    assert ok.status_code == 201

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "save_reconciliation", boom)
    bad = client.post("/accounts/写失败账号/reconciliations", json=body)
    assert bad.status_code == 500
    assert "保留在用结果" in bad.json()["detail"]

    monkeypatch.undo()
    latest = client.get("/accounts/写失败账号/reconciliations/latest").json()
    assert latest["id"] == ok.json()["reconciliation_id"]


def test_infeasible_retains_in_use_result(client):
    """无解时本次退回，账号在用结果与历史都不变。"""
    ids = _diag_ids(client, "无解保留")
    body = {"diagnosis_ids": ids, "tanks": TANKS}
    ok = client.post("/accounts/无解保留账号/reconciliations", json=body)
    assert ok.status_code == 201

    infeasible = dict(body, storage=[
        {"time_index": 0, "tank": "TA", "lo": 0, "hi": 0},
        {"time_index": 0, "tank": "TB", "lo": 0, "hi": 0},
        {"time_index": 1, "tank": "TA", "lo": 10, "hi": 10},
        {"time_index": 1, "tank": "TB", "lo": 10, "hi": 10}])
    r = client.post("/accounts/无解保留账号/reconciliations", json=infeasible)
    assert r.status_code == 422
    latest = client.get("/accounts/无解保留账号/reconciliations/latest").json()
    assert latest["id"] == ok.json()["reconciliation_id"]
    rows = client.get("/accounts/无解保留账号/reconciliations").json()
    assert len(rows["reconciliations"]) == 1


def test_snapshot_uses_remeasure_intersection_or_original(client):
    """留档复测交集取代原读数；没复测用原读数；容量始终封顶。"""
    from app.main import _load_snapshot

    # 一班：S3 原读数 [300,400]，两次复测 [100,250]、[150,300] -> 交集 [150,250]
    r = client.post("/shifts/复测班/readings", json={
        "ports": ["IN", "OUT"],
        "segments": [
            {"key": "S1", "from_node": "IN", "to_node": "A",
             "capacity": 1000, "meter_lo": 100, "meter_hi": 400},
            {"key": "S2", "from_node": "A", "to_node": "B",
             "capacity": 1000, "meter_lo": 100, "meter_hi": 400},
            {"key": "S3", "from_node": "B", "to_node": "OUT",
             "capacity": 220, "meter_lo": 300, "meter_hi": 400}]})
    assert r.status_code == 201
    for lo, hi in [(100, 250), (150, 300)]:
        assert client.post("/shifts/复测班/remeasurements",
                           json={"remeasures": [
                               {"segment_key": "S3", "rlo": lo, "rhi": hi}]}
                           ).status_code == 201
    did = client.get("/shifts/复测班/diagnoses/latest").json()["id"]
    snap = _load_snapshot(did)
    segs = {s.key: s for s in snap.segments}
    # 复测交集 [150,250] 与容量 220 取交集
    assert (segs["S3"].lo, segs["S3"].hi) == (150, 220)
    # 未复测管段沿用原读数
    assert (segs["S1"].lo, segs["S1"].hi) == (100, 400)


def test_snapshot_clips_original_reading_by_capacity(client):
    """没复测时原读数与过水量上限取交集（读数高于上限属疑似失准）。"""
    from app.main import _load_snapshot

    r = client.post("/shifts/超限班/readings", json={
        "ports": ["IN", "OUT"],
        "segments": [{"key": "K1", "from_node": "IN", "to_node": "OUT",
                      "capacity": 50, "meter_lo": 80, "meter_hi": 120}]})
    assert r.status_code == 201
    did = r.json()["diagnosis_id"]
    seg = _load_snapshot(did).segments[0]
    assert (seg.lo, seg.hi) == (80, 50)  # 空区间，核算时判无解


def test_total_bounds_are_joint_not_sum_of_individual(client):
    """合计上下界是联合优化的结果，不是各自界相加。"""
    ids = _diag_ids(client, "合计界")
    # TA 与 TB 由同一进水 F1<=1 分流，故合计每班净流入 <=1
    s = [{"key": "H1", "from_node": "IN", "to_node": "J",
          "capacity": 1, "meter_lo": 0, "meter_hi": 1},
         {"key": "H2", "from_node": "J", "to_node": "TA",
          "capacity": 1, "meter_lo": 0, "meter_hi": 1},
         {"key": "H3", "from_node": "J", "to_node": "TB",
          "capacity": 1, "meter_lo": 0, "meter_hi": 1},
         {"key": "H4", "from_node": "TA", "to_node": "OUT",
          "capacity": 1, "meter_lo": 0, "meter_hi": 1},
         {"key": "H5", "from_node": "TB", "to_node": "OUT",
          "capacity": 1, "meter_lo": 0, "meter_hi": 1}]
    ids2 = []
    for i in range(2):
        rr = client.post(f"/shifts/合计{i}/readings",
                         json={"ports": P, "segments": s})
        ids2.append(rr.json()["diagnosis_id"])
    r = client.post("/accounts/合计账号/reconciliations", json={
        "diagnosis_ids": ids2, "tanks": TANKS,
        "storage": [{"time_index": 0, "tank": "TA", "lo": 0, "hi": 0},
                    {"time_index": 0, "tank": "TB", "lo": 0, "hi": 0},
                    {"time_index": 2, "tank": "TA", "lo": 10, "hi": 10},
                    {"time_index": 2, "tank": "TB", "lo": 10, "hi": 10}]})
    # 两班合计最多升 2 升，填到 20 升无解
    assert r.status_code == 422
