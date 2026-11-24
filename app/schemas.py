"""请求体的 Pydantic 校验模型。水量一律为非负整数升。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_SEGMENTS = 20
_NAME = Field(min_length=1, max_length=50)


class SegmentIn(BaseModel):
    key: str = _NAME
    from_node: str = _NAME
    to_node: str = _NAME
    capacity: int = Field(ge=0, description="过水量上限（升）")
    meter_lo: int = Field(ge=0, description="水表读数区间下界（升）")
    meter_hi: int = Field(ge=0, description="水表读数区间上界（升）")

    @model_validator(mode="after")
    def _interval_ok(self):
        if self.meter_lo > self.meter_hi:
            raise ValueError("水表读数区间下界不能大于上界")
        # 注意：读数区间高于过水量上限属于“疑似失准”，照常接收，
        # 交由诊断判定（求解时与上限取交集）。
        return self


class ReadingsIn(BaseModel):
    ports: list[str] = Field(default_factory=list,
                             description="进出水口节点（不要求进出平衡）")
    segments: list[SegmentIn] = Field(min_length=1, max_length=MAX_SEGMENTS)

    @field_validator("ports")
    @classmethod
    def _ports_ok(cls, v):
        for p in v:
            if not p or len(p) > 50:
                raise ValueError("进出水口名称须为 1~50 字符")
        if len(set(v)) != len(v):
            raise ValueError("进出水口名称重复")
        return v

    @model_validator(mode="after")
    def _keys_unique(self):
        keys = [s.key for s in self.segments]
        if len(set(keys)) != len(keys):
            raise ValueError("管段编号重复")
        return self


class RemeasureIn(BaseModel):
    segment_key: str = _NAME
    rlo: int = Field(ge=0, description="复测区间下界（升）")
    rhi: int = Field(ge=0, description="复测区间上界（升）")

    @model_validator(mode="after")
    def _interval_ok(self):
        if self.rlo > self.rhi:
            raise ValueError("复测区间下界不能大于上界")
        return self


class RemeasureBatch(BaseModel):
    remeasures: list[RemeasureIn] = Field(min_length=1, max_length=MAX_SEGMENTS)


# ---------------- 多班水箱存量核算 ----------------

MIN_RECONCILE_SHIFTS = 2
MAX_RECONCILE_SHIFTS = 6
RECONCILE_TANKS = 2


class TankIn(BaseModel):
    port: str = _NAME
    capacity: int = Field(ge=0, description="水箱容量上限（升）")


class StorageReadingIn(BaseModel):
    time_index: int = Field(ge=0, description="时点序号：0 为首班期初，n 为末班期末")
    tank: str = _NAME
    lo: int = Field(ge=0, description="实测存量下界（升）")
    hi: int = Field(ge=0, description="实测存量上界（升）")

    @model_validator(mode="after")
    def _interval_ok(self):
        if self.lo > self.hi:
            raise ValueError("实测存量区间下界不能大于上界")
        return self


class ReconciliationIn(BaseModel):
    diagnosis_ids: list[int] = Field(
        min_length=MIN_RECONCILE_SHIFTS, max_length=MAX_RECONCILE_SHIFTS,
        description="按交接顺序排列的 2~6 个不同班次的诊断号")
    tanks: list[TankIn] = Field(
        min_length=RECONCILE_TANKS, max_length=RECONCILE_TANKS,
        description="恰好两支水箱口及各自容量")
    storage: list[StorageReadingIn] = Field(
        default_factory=list,
        description="任一时点的实测存量区间（可含全段首尾）")

    @field_validator("diagnosis_ids")
    @classmethod
    def _diag_ids_ok(cls, v):
        if any(i <= 0 for i in v):
            raise ValueError("诊断号须为正整数")
        if len(set(v)) != len(v):
            raise ValueError("诊断号重复")
        return v

    @model_validator(mode="after")
    def _shape_ok(self):
        ports = [t.port for t in self.tanks]
        if len(set(ports)) != len(ports):
            raise ValueError("水箱口名称重复")
        seen: set[tuple[int, str]] = set()
        for s in self.storage:
            if s.tank not in ports:
                raise ValueError(f"实测存量对象不是登记的水箱口：{s.tank}")
            key = (s.time_index, s.tank)
            if key in seen:
                raise ValueError(f"同一时点同一水箱只能填写一个实测区间："
                                 f"{s.time_index}/{s.tank}")
            seen.add(key)
        return self
