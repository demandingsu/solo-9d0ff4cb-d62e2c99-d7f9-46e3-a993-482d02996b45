"""多班交接的水箱存量核算核心求解器。

把按交接顺序排列的若干班次诊断快照串成一条“整段”时间轴：每个诊断
快照贡献一组管段流量变量（沿用该快照的过水量上限、水表读数／留档复测
区间以及普通节点守恒规则），相邻班次之间共用交接时点的水箱存量。

对第 i 班（0 起）与水箱 t：

    H[i+1][t] - H[i][t] = 该班该箱净流入
                        = Σ(流入该箱的管段流量) - Σ(流出该箱的管段流量)

其中两支水箱在各班快照中都必须登记为进出水口（因此不参与普通节点守恒，
其存量变化由上式单独刻画）。所有时点的存量都必须在 [0, 容量] 之间；
可另给任一时点的实测存量区间。

求解仍用混合整数规划（HiGHS，经 scipy.optimize.milp），全部流量与存量
都是整数升。先求一个整体可行解；再对每个时点、每支水箱及两箱合计分别
求最小／最大值，得到整段约束下的可达上下界。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from .solver import SolverError


class ReconciliationInfeasible(RuntimeError):
    """整段约束无解（区别于求解器自身失败）。调用方应保留在用结果。"""


@dataclass(frozen=True)
class FlowSegment:
    key: str
    src: str
    dst: str
    lo: int          # 生效下界（容量 ∩ 原读数／留档复测交集）
    hi: int          # 生效上界


@dataclass(frozen=True)
class ShiftSnapshot:
    diagnosis_id: int
    shift: str
    ports: frozenset[str]
    segments: tuple[FlowSegment, ...]


def _solve(a: np.ndarray, lb: np.ndarray, ub: np.ndarray,
           objective: np.ndarray) -> np.ndarray | None:
    """求解一次 MILP：返回整数可行解；不可行返回 None；其余状态抛 SolverError。"""
    if np.any(lb > ub):
        return None
    res = milp(c=objective,
               constraints=[LinearConstraint(a, 0.0, 0.0)],
               integrality=np.ones(len(lb)),
               bounds=Bounds(lb, ub))
    if res.status == 0:
        return np.rint(res.x).astype(np.int64)
    if res.status == 2:
        return None
    raise SolverError(f"MILP 求解失败：status={res.status} {res.message}")


def reconcile(snapshots: list[ShiftSnapshot],
              tanks: list[tuple[str, int]],
              storage_limits: dict[tuple[int, str], tuple[int, int]]) -> dict:
    """核算整段两箱存量可达上下界。

    snapshots: 按交接顺序排列的班次快照（2~6 个）。
    tanks: [(水箱口名称, 容量升), (…)]，恰好两支。
    storage_limits: (time_index, 水箱名) -> (下界, 上界)，可选。

    返回 {"shifts", "tanks", "time_points", "witness"}；
    整段不可行时抛 ReconciliationInfeasible。
    """
    tank_names = [name for name, _ in tanks]
    tank_caps = [int(cap) for _, cap in tanks]
    n_shifts = len(snapshots)
    n_tanks = len(tanks)

    # ---- 变量布局：各班的管段流量，然后各时点各箱的存量 ----
    flow_off: list[int] = []
    n_flow = 0
    for snap in snapshots:
        flow_off.append(n_flow)
        n_flow += len(snap.segments)
    storage_idx = [[0] * n_tanks for _ in range(n_shifts + 1)]
    cursor = n_flow
    for t in range(n_shifts + 1):
        for k in range(n_tanks):
            storage_idx[t][k] = cursor
            cursor += 1
    n_vars = cursor

    lb = np.zeros(n_vars, dtype=float)
    ub = np.zeros(n_vars, dtype=float)

    # ---- 管段流量上下界：容量始终有效，复测值取代原读数 ----
    for i, snap in enumerate(snapshots):
        for j, seg in enumerate(snap.segments):
            v = flow_off[i] + j
            lb[v] = max(0, seg.lo)
            ub[v] = max(0, seg.hi)

    # ---- 存量上下界：[0, 容量] 与可选实测区间取交集 ----
    for t in range(n_shifts + 1):
        for k, name in enumerate(tank_names):
            v = storage_idx[t][k]
            lo, hi = 0.0, float(tank_caps[k])
            limit = storage_limits.get((t, name))
            if limit is not None:
                lo = max(lo, float(limit[0]))
                hi = min(hi, float(limit[1]))
            lb[v] = lo
            ub[v] = hi

    # ---- 等式约束 ----
    rows: list[np.ndarray] = []

    def add_row(coefficients: dict[int, float]) -> None:
        row = np.zeros(n_vars, dtype=float)
        for var, coef in coefficients.items():
            row[var] += coef
        rows.append(row)

    for i, snap in enumerate(snapshots):
        segs = snap.segments
        base = flow_off[i]

        # 普通节点守恒：非进出水口节点 流入-流出=0
        nodes: set[str] = set()
        for seg in segs:
            nodes.add(seg.src)
            nodes.add(seg.dst)
        for node in sorted(nodes - set(snap.ports)):
            coefs: dict[int, float] = {}
            for j, seg in enumerate(segs):
                if seg.dst == node:
                    coefs[base + j] = coefs.get(base + j, 0.0) + 1.0
                if seg.src == node:
                    coefs[base + j] = coefs.get(base + j, 0.0) - 1.0
            add_row(coefs)

        # 每班每箱：期末-期初 = 该口净流入
        for k, name in enumerate(tank_names):
            coefs = {storage_idx[i + 1][k]: 1.0,
                     storage_idx[i][k]: -1.0}
            for j, seg in enumerate(segs):
                if seg.dst == name:      # 流入该箱，净流入 +x
                    coefs[base + j] = coefs.get(base + j, 0.0) - 1.0
                if seg.src == name:      # 流出该箱，净流入 -x
                    coefs[base + j] = coefs.get(base + j, 0.0) + 1.0
            add_row(coefs)

    a = np.vstack(rows)

    # ---- 先确认整段可行 ----
    witness = _solve(a, lb, ub, np.zeros(n_vars))
    if witness is None:
        raise ReconciliationInfeasible("整段约束无解")

    # ---- 逐时点、逐箱、合计求上下界 ----
    time_points = []
    for t in range(n_shifts + 1):
        bounds: dict[str, dict[str, int]] = {}
        for k, name in enumerate(tank_names):
            objective = np.zeros(n_vars)
            objective[storage_idx[t][k]] = 1.0
            x_min = _solve(a, lb, ub, objective)
            x_max = _solve(a, lb, ub, -objective)
            if x_min is None or x_max is None:  # 已确认可行，不应再不可行
                raise SolverError("求存量可行界时求解器状态异常")
            bounds[name] = {"min": int(x_min[storage_idx[t][k]]),
                            "max": int(x_max[storage_idx[t][k]])}
        total = np.zeros(n_vars)
        for k in range(n_tanks):
            total[storage_idx[t][k]] = 1.0
        x_min = _solve(a, lb, ub, total)
        x_max = _solve(a, lb, ub, -total)
        if x_min is None or x_max is None:
            raise SolverError("求合计存量可行界时求解器状态异常")
        bounds["total"] = {
            "min": int(sum(x_min[storage_idx[t][k]] for k in range(n_tanks))),
            "max": int(sum(x_max[storage_idx[t][k]] for k in range(n_tanks))),
        }
        time_points.append({
            "time_index": t,
            "measured": {name: ({"lo": storage_limits[(t, name)][0],
                                 "hi": storage_limits[(t, name)][1]}
                                if (t, name) in storage_limits else None)
                         for name in tank_names},
            "bounds": bounds,
        })

    witness_storage = []
    for t in range(n_shifts + 1):
        point = {"time_index": t}
        for k, name in enumerate(tank_names):
            point[name] = int(witness[storage_idx[t][k]])
        point["total"] = int(sum(witness[storage_idx[t][k]]
                                 for k in range(n_tanks)))
        witness_storage.append(point)

    witness_flows = []
    for i, snap in enumerate(snapshots):
        witness_flows.append({
            "diagnosis_id": snap.diagnosis_id,
            "shift": snap.shift,
            "segments": {seg.key: int(witness[flow_off[i] + j])
                         for j, seg in enumerate(snap.segments)},
        })

    return {
        "shifts": [{"diagnosis_id": s.diagnosis_id, "shift": s.shift}
                   for s in snapshots],
        "tanks": [{"port": name, "capacity": cap}
                  for name, cap in zip(tank_names, tank_caps)],
        "time_points": time_points,
        "witness": {"storage": witness_storage, "flows": witness_flows},
    }
