"""HTTP 请求 / 响应的 Pydantic 模型与输入校验。

校验失败（非法 ID、非法区间、非法冲突对等）由 FastAPI 统一返回 422。
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator


class Flow(BaseModel):
    """一条有向车流及其可接受的整数秒绿灯区间。"""

    model_config = ConfigDict(extra="forbid")

    id: StrictStr = Field(min_length=1, description="车流唯一 ID，非空字符串")
    min_green: int = Field(ge=1, le=60, description="绿灯下限（秒），1~60")
    max_green: int = Field(ge=1, le=60, description="绿灯上限（秒），1~60")


class PlanRequest(BaseModel):
    """POST /plans 请求体。"""

    model_config = ConfigDict(extra="forbid")

    flows: list[Flow] = Field(min_length=2, max_length=10, description="2~10 条车流")
    conflicts: list[list[StrictStr]] = Field(
        default_factory=list, description="无向冲突对 [id_a, id_b]，同相位不得共存"
    )
    clearances: dict[StrictStr, Annotated[int, Field(ge=0, le=10)]] = Field(
        default_factory=dict,
        description="有向清空时间，键 'id_a->id_b'，值 0~10 秒；未列出按 0 处理",
    )
    cycle_limit: int = Field(ge=1, description="周期上限（正整数秒）")

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> "PlanRequest":
        ids = [f.id for f in self.flows]
        id_set = set(ids)

        if len(id_set) != len(ids):
            raise ValueError("flows 中的 id 必须唯一")

        for f in self.flows:
            if f.min_green > f.max_green:
                raise ValueError(
                    f"车流 {f.id} 的 min_green({f.min_green}) 不能大于 "
                    f"max_green({f.max_green})"
                )

        seen_conflicts: set[tuple[str, str]] = set()
        for pair in self.conflicts:
            if len(pair) != 2:
                raise ValueError(f"冲突对必须恰好包含两个车流 ID：{pair!r}")
            a, b = pair
            if a not in id_set or b not in id_set:
                raise ValueError(f"冲突对引用了不存在的车流 ID：[{a!r}, {b!r}]")
            if a == b:
                raise ValueError(f"车流不能与自身冲突：{a!r}")
            key = tuple(sorted((a, b)))
            if key in seen_conflicts:
                raise ValueError(f"冲突对重复（无向）：[{a!r}, {b!r}]")
            seen_conflicts.add(key)

        for key in self.clearances:
            parts = key.split("->")
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise ValueError(
                    f"清空时间键格式非法：{key!r}，应为 'id_a->id_b'"
                )
            a, b = parts
            if a not in id_set or b not in id_set:
                raise ValueError(f"清空时间键引用了不存在的车流 ID：{key!r}")

        return self


class Phase(BaseModel):
    """一个放行相位。"""

    order: int = Field(ge=1, description="相位顺序，从 1 开始")
    flows: list[str] = Field(description="本相位放行的车流 ID")
    green: int = Field(ge=1, le=60, description="本相位绿灯时长（秒）")
    all_red_after: int = Field(
        ge=0, le=10, description="本相位之后、下一相位之前的全红时长（含末相位到首相位）"
    )


class Plan(BaseModel):
    """编排结果。"""

    phases: list[Phase]
    cycle_length: int = Field(ge=1, description="周期总秒数 = 各相位绿灯 + 各段全红")
    phase_count: int = Field(ge=1, le=10, description="相位数")


SolveStatus = Literal["optimal", "feasible_candidate", "infeasible", "unknown"]


class PlanResponse(BaseModel):
    """响应体：

    - optimal：已证明周期最短（并列最短时相位数最少）
    - feasible_candidate：找到可行候选，但时限内未证明最优
    - infeasible：已证明周期上限内无解
    - unknown：时限内既未找到可行解，也未能证明无解
    """

    status: SolveStatus
    plan: Plan | None = None
