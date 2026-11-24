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
