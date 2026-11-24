"""管网水量平衡诊断核心求解器。

建模：每个管段的实际过水量为非负整数变量（单位：升），约束为
  * 除标记的进出水口外，每个节点流入量 = 流出量；
  * 0 <= 流量 <= 过水量上限（始终有效）；
  * 水表读数区间（诊断“单表失准”候选时可临时忽略某一只表的原读数）；
  * 人工复测区间：以人工复测值取代该表原读数，且始终有效——
    即使被忽略的表已经复测，其复测区间也不被忽略；
    同一管段多次复测取交集（在 API 层合并后传入）。

节点-弧关联矩阵配合整数上下界，用混合整数规划（HiGHS，经 scipy.optimize.milp）
精确求解，流量保证为整数升。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp


class SolverError(RuntimeError):
    """求解器自身失败（区别于“约束不可行”）。调用方应据此保留旧数据。"""


@dataclass(frozen=True)
class Segment:
    key: str
    src: str
    dst: str
    capacity: int
    meter_lo: int
    meter_hi: int


def _balance_matrix(segments: list[Segment], ports: set[str]) -> np.ndarray | None:
    """内部节点的节点-弧关联矩阵；行约束为 流入-流出=0。无内部节点时返回 None。"""
    nodes = {s.src for s in segments} | {s.dst for s in segments}
    internal = sorted(nodes - ports)
    if not internal:
        return None
    row = {n: i for i, n in enumerate(internal)}
    a = np.zeros((len(internal), len(segments)))
    for j, s in enumerate(segments):
        if s.src in row:
            a[row[s.src], j] -= 1.0  # 流出
        if s.dst in row:
            a[row[s.dst], j] += 1.0  # 流入
    return a


def _solve(a: np.ndarray | None, lb: np.ndarray, ub: np.ndarray,
           objective: np.ndarray | None = None) -> np.ndarray | None:
    """返回一个整数可行解；不可行返回 None；求解器异常抛 SolverError。"""
    n = len(lb)
    if np.any(lb > ub):
        return None  # 区间已为空，必然不可行
    c = np.zeros(n) if objective is None else objective
    constraints = [] if a is None else [LinearConstraint(a, 0.0, 0.0)]
    res = milp(c=c, constraints=constraints, integrality=np.ones(n),
               bounds=Bounds(lb, ub))
    if res.status == 0:
        return np.rint(res.x).astype(int)
    if res.status == 2:
        return None  # 不可行
    raise SolverError(f"MILP 求解失败：status={res.status} {res.message}")


def diagnose(segments: list[Segment], ports: set[str],
             remeasure: dict[str, tuple[int, int]]) -> dict:
    """对一班登记数据做账平诊断。

    remeasure: segment_key -> (下界, 上界)，调用前需保证 key 均存在。
    返回 dict：
      balanced=True  -> {"balanced": True, "assignment": {key: 流量}}
      balanced=False -> {"balanced": False, "suspects": [{segment_key,
                          feasible_min, feasible_max}, ...]}
    """
    n = len(segments)
    a = _balance_matrix(segments, ports)
    caps = np.array([s.capacity for s in segments], dtype=float)
    index = {s.key: i for i, s in enumerate(segments)}
    rem = [(index[k], float(lo), float(hi)) for k, (lo, hi) in remeasure.items()]

    def bounds(ignore: int | None) -> tuple[np.ndarray, np.ndarray]:
        lb = np.zeros(n)
        ub = caps.copy()
        rem_keys = {idx for idx, _, _ in rem}
        for i, s in enumerate(segments):
            if i == ignore and i not in rem_keys:
                continue  # 仅忽略该表原读数；上限仍有效
            if i in rem_keys:
                continue  # 复测值取代原读数，下面统一施加
            lb[i] = max(lb[i], float(s.meter_lo))
            ub[i] = min(ub[i], float(s.meter_hi))
        for idx, lo, hi in rem:  # 复测区间始终有效，不被忽略
            lb[idx] = max(lb[idx], lo)
            ub[idx] = min(ub[idx], hi)
        return lb, ub

    lb, ub = bounds(ignore=None)
    x = _solve(a, lb, ub)
    if x is not None:
        return {"balanced": True,
                "assignment": {s.key: int(v) for s, v in zip(segments, x)}}

    suspects = []
    for i, s in enumerate(segments):
        lb_i, ub_i = bounds(ignore=i)
        if _solve(a, lb_i, ub_i) is None:
            continue
        e = np.zeros(n)
        e[i] = 1.0
        x_min = _solve(a, lb_i, ub_i, objective=e)
        x_max = _solve(a, lb_i, ub_i, objective=-e)
        if x_min is None or x_max is None:  # 已确认可行，不应再不可行
            raise SolverError("求管段可行界时求解器状态异常")
        suspects.append({"segment_key": s.key,
                         "feasible_min": int(x_min[i]),
                         "feasible_max": int(x_max[i])})
    return {"balanced": False, "suspects": suspects}
