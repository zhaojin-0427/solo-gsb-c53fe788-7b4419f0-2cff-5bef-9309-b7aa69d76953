"""HTTP 请求 / 响应的 Pydantic 模型与输入校验。

校验失败（非法 ID、非法区间、非法冲突对等）由 FastAPI 统一返回 422。
"""

from typing import Annotated, Literal, Optional

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
    flows: list[str] = Field(min_length=1, max_length=10, description="本相位放行的车流 ID")
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


class TravelInterval(BaseModel):
    """相邻路口之间的整数秒行驶时间闭区间。"""

    model_config = ConfigDict(extra="forbid")

    min_travel: int = Field(ge=0, description="最小行驶时间（含），非负整数秒")
    max_travel: int = Field(ge=0, description="最大行驶时间（含），非负整数秒")


class CoordinationRoute(BaseModel):
    """按路口顺序引用各路口车流 ID 的目标路线。"""

    model_config = ConfigDict(extra="forbid")

    id: Optional[StrictStr] = Field(
        default=None,
        min_length=1,
        description="路线 ID；省略时按顺序使用 R1、R2...",
    )
    flow_ids: list[StrictStr] = Field(
        min_length=2, max_length=8, description="每个路口对应一个车流 ID，顺序与 plans 相同"
    )
    weight: int = Field(ge=1, le=1_000_000_000, description="路线协调权重，正整数")


class CoordinationRequest(BaseModel):
    """POST /coordination 请求体。"""

    model_config = ConfigDict(extra="forbid")

    plans: list[Plan] = Field(
        min_length=2,
        max_length=8,
        description="2～8 个已生成、行驶方向排列的路口方案",
    )
    travel_intervals: list[TravelInterval] = Field(
        min_length=1, max_length=7, description="相邻路口行驶时间区间，数量为路口数 - 1"
    )
    routes: list[CoordinationRoute] = Field(
        min_length=1, max_length=1000, description="待协调目标路线"
    )

    @model_validator(mode="after")
    def _validate_coordination(self) -> "CoordinationRequest":
        cycles = {plan.cycle_length for plan in self.plans}
        if len(cycles) != 1:
            raise ValueError("所有路口方案的 cycle_length 必须一致")

        for intersection_index, plan in enumerate(self.plans):
            if len(plan.phases) != plan.phase_count:
                raise ValueError(
                    f"路口 {intersection_index + 1} 的 phases 数量({len(plan.phases)})"
                    f"必须等于 phase_count({plan.phase_count})"
                )
            orders = [phase.order for phase in plan.phases]
            if orders != list(range(1, plan.phase_count + 1)):
                raise ValueError(
                    f"路口 {intersection_index + 1} 的 phase.order 必须从 1 开始连续且按顺序排列"
                )
            flow_ids = [flow_id for phase in plan.phases for flow_id in phase.flows]
            if len(set(flow_ids)) != len(flow_ids):
                raise ValueError(f"路口 {intersection_index + 1} 内的车流 ID 不得跨相位重复")
            if plan.phase_count == 1 and plan.phases[0].all_red_after != 0:
                raise ValueError(
                    f"路口 {intersection_index + 1} 只有一个相位时 "
                    "all_red_after 必须为 0"
                )
            phase_duration = sum(
                phase.green + (phase.all_red_after if plan.phase_count > 1 else 0)
                for phase in plan.phases
            )
            if phase_duration != plan.cycle_length:
                raise ValueError(
                    f"路口 {intersection_index + 1} 的相位绿灯和全红之和必须等于 cycle_length"
                )

        expected_segments = len(self.plans) - 1
        if len(self.travel_intervals) != expected_segments:
            raise ValueError(
                f"travel_intervals 数量必须为路口数 - 1（期望 {expected_segments}）"
            )
        for segment_index, interval in enumerate(self.travel_intervals):
            if interval.min_travel > interval.max_travel:
                raise ValueError(
                    f"相邻路口 {segment_index + 1}->{segment_index + 2} 的 "
                    "min_travel 不能大于 max_travel"
                )

        route_ids = [route.id for route in self.routes if route.id is not None]
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("routes[].id 必须唯一")

        for route_index, route in enumerate(self.routes):
            route_label = route.id or f"R{route_index + 1}"
            if len(route.flow_ids) != len(self.plans):
                raise ValueError(
                    f"路线 {route_label} 的 flow_ids 数量必须等于路口数 {len(self.plans)}"
                )
            for intersection_index, flow_id in enumerate(route.flow_ids):
                available = {
                    flow
                    for phase in self.plans[intersection_index].phases
                    for flow in phase.flows
                }
                if flow_id not in available:
                    raise ValueError(
                        f"路线 {route_label} 在路口 {intersection_index + 1} "
                        f"引用了不存在的车流 ID：{flow_id!r}"
                    )

        return self


class DepartureWindow(BaseModel):
    """相对首个路口周期开始时间的可通行出发秒闭区间。"""

    start: int = Field(ge=0, description="窗口起始秒，含")
    end: int = Field(ge=0, description="窗口结束秒，含；wraps=true 时窗口跨周期结束")
    wraps: bool = Field(description="是否为 start..C-1 与 0..end 组成的跨周期窗口")


class RouteCoordinationResult(BaseModel):
    """单条路线在最终偏移下的协调结果。"""

    id: str
    weight: int = Field(ge=1, description="路线权重")
    coordinated: bool
    departure_windows: list[DepartureWindow] = Field(
        default_factory=list, description="可连续通过的统一出发秒窗口；未协调时为空"
    )
    reason: Optional[
        Literal[
            "green_window_too_short",
            "no_compatible_offset",
            "solver_incomplete",
        ]
    ] = None


class CoordinationResponse(BaseModel):
    """绿波协调结果。"""

    status: SolveStatus
    cycle_length: int = Field(ge=1, description="共同周期（秒）")
    offsets: Optional[list[int]] = Field(
        default=None, description="各路口相对偏移；首个为 0，unknown 时可能为 null"
    )
    coordinated_weight: int = Field(ge=0, description="最终可连续通过路线的权重总和")
    routes: list[RouteCoordinationResult]
