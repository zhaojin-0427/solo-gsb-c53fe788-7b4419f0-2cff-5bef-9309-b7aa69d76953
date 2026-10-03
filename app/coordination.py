"""多路口绿波协调：Pydantic 模型、输入校验与基于 OR-Tools CP-SAT 的偏移求解。

输入 K（2~8）个按行驶方向排列、周期相同的已生成路口方案（与 POST /plans
响应中的 plan 同构，外加路口 id）、K-1 个相邻路口整数行驶时间闭区间，以及
由各路口车流 ID 组成、带正整数权重的目标路线。

决策：每个路口相对首个路口的整数秒偏移 o[k] ∈ [0, C-1]，o[0] 固定为 0。

一条路线可连续通过（coordinated）当且仅当存在一个统一的出发秒 d ∈ [0,C-1]，
使得对每个路口 k，按各段行驶时间区间任意组合得到的所有到达时刻
（d + Σ_{j<k}(L_j + t_j)，t_j ∈ [0, Δ_j]）模 C 后都落在该路口对应车流的
绿灯窗口内。

优化目标（字典序）：
1. 可连续通过路线的权重总和最大；
2. 总偏移量 Σ o[k] 最小；
3. 偏移向量 (o[0],…,o[K-1]) 字典序最小（o[0] 恒为 0，故从 o[1] 起）。

状态语义与 /plans 一致：optimal / feasible_candidate / infeasible / unknown。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Annotated, Literal

from ortools.sat.python import cp_model
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

_NonEmptyId = Annotated[str, Field(min_length=1)]


class CoordinationPhase(BaseModel):
    """与 /plans 响应中的相位同构。"""

    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1, description="相位顺序，从 1 开始")
    flows: list[_NonEmptyId] = Field(min_length=1, description="本相位放行的车流 ID")
    green: int = Field(ge=1, le=60, description="绿灯时长（秒）")
    all_red_after: int = Field(ge=0, le=10, description="本相位之后的全红时长（秒）")


class CoordinationIntersection(BaseModel):
    """一个已生成的路口方案（与 /plans 响应中的 plan 同构，外加 id）。"""

    model_config = ConfigDict(extra="forbid")

    id: StrictStr = Field(min_length=1, description="路口唯一 ID")
    phases: list[CoordinationPhase] = Field(min_length=1, max_length=10)
    cycle_length: int = Field(ge=1, description="周期总秒数")
    phase_count: int = Field(ge=1, le=10, description="相位数")


class TravelTime(BaseModel):
    """相邻路口之间的整数行驶时间闭区间 [min_seconds, max_seconds]（秒）。"""

    model_config = ConfigDict(extra="forbid")

    min_seconds: int = Field(ge=0, le=3600, description="行驶时间下限（含，秒）")
    max_seconds: int = Field(ge=0, le=3600, description="行驶时间上限（含，秒）")


class RouteSpec(BaseModel):
    """一条目标路线：依次经过各路口时所借用的车流 ID 与正整数权重。"""

    model_config = ConfigDict(extra="forbid")

    id: StrictStr = Field(min_length=1, description="路线唯一 ID")
    weight: int = Field(ge=1, le=1_000_000_000, description="正整数权重")
    flows: list[_NonEmptyId] = Field(description="按路口顺序给出的车流 ID，每路口一个")


class CoordinationRequest(BaseModel):
    """POST /coordination 请求体。"""

    model_config = ConfigDict(extra="forbid")

    intersections: list[CoordinationIntersection] = Field(
        min_length=2, max_length=8, description="2~8 个按行驶方向排列的路口方案"
    )
    travel_times: list[TravelTime] = Field(
        min_length=1, max_length=7, description="K-1 个相邻路口行驶时间闭区间"
    )
    routes: list[RouteSpec] = Field(
        min_length=1, max_length=32, description="至少一条带正整数权重的目标路线"
    )

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> "CoordinationRequest":
        k = len(self.intersections)

        if len(self.travel_times) != k - 1:
            raise ValueError(
                f"travel_times 数量必须为路口数减 1：{k} 个路口需要 {k - 1} 个，"
                f"实际 {len(self.travel_times)} 个"
            )
        for j, tt in enumerate(self.travel_times):
            if tt.min_seconds > tt.max_seconds:
                raise ValueError(
                    f"第 {j + 1} 段行驶时间下限({tt.min_seconds})不能大于"
                    f"上限({tt.max_seconds})"
                )

        ix_ids = [ix.id for ix in self.intersections]
        if len(set(ix_ids)) != k:
            raise ValueError("intersections 中的路口 id 必须唯一")

        common_cycle = self.intersections[0].cycle_length
        flow_maps: list[dict[str, CoordinationPhase]] = []
        for ix in self.intersections:
            if ix.cycle_length != common_cycle:
                raise ValueError(
                    f"路口 {ix.id!r} 的周期 {ix.cycle_length} 与首个路口的周期 "
                    f"{common_cycle} 不一致，协调只支持相同周期"
                )
            if ix.phase_count != len(ix.phases):
                raise ValueError(
                    f"路口 {ix.id!r} 的 phase_count={ix.phase_count} 与实际相位"
                    f"数 {len(ix.phases)} 不一致"
                )
            orders = [p.order for p in ix.phases]
            if sorted(orders) != list(range(1, len(ix.phases) + 1)):
                raise ValueError(
                    f"路口 {ix.id!r} 的相位 order 必须恰好为 1..{len(ix.phases)} "
                    f"且不重复，实际 {sorted(orders)}"
                )
            by_flow: dict[str, CoordinationPhase] = {}
            for p in ix.phases:
                for fid in p.flows:
                    if fid in by_flow:
                        raise ValueError(
                            f"路口 {ix.id!r} 中车流 {fid!r} 出现在多个相位"
                        )
                    by_flow[fid] = p
            flow_maps.append(by_flow)
            if len(ix.phases) == 1 and ix.phases[0].all_red_after != 0:
                raise ValueError(
                    f"路口 {ix.id!r} 只有一个相位时 all_red_after 必须为 0"
                )
            # 相位绿灯 + 全红之和必须等于周期（校验方案内部自洽）。
            total = sum(p.green + p.all_red_after for p in ix.phases)
            if total != ix.cycle_length:
                raise ValueError(
                    f"路口 {ix.id!r} 各相位绿灯与全红之和 {total} 不等于周期 "
                    f"{ix.cycle_length}"
                )

        route_ids = [r.id for r in self.routes]
        if len(set(route_ids)) != len(route_ids):
            raise ValueError("routes 中的路线 id 必须唯一")
        for r in self.routes:
            if len(r.flows) != k:
                raise ValueError(
                    f"路线 {r.id!r} 的 flows 长度必须等于路口数 {k}，"
                    f"实际 {len(r.flows)}"
                )
            for pos, fid in enumerate(r.flows):
                if fid not in flow_maps[pos]:
                    raise ValueError(
                        f"路线 {r.id!r} 的第 {pos + 1} 个车流 {fid!r} 在路口 "
                        f"{self.intersections[pos].id!r} 的方案中不存在"
                    )

        return self


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------

CoordinationStatus = Literal[
    "optimal", "feasible_candidate", "infeasible", "unknown"
]


class RouteCoordinationResult(BaseModel):
    """单条路线的协调结果。"""

    id: str
    weight: int = Field(ge=1)
    coordinated: bool = Field(description="在返回偏移下是否存在统一出发秒连续通过")
    departure_windows: list[list[int]] = Field(
        description=(
            "可通行出发秒窗口，每项为闭区间 [start, end]（含端点，不跨周期边界）；"
            "coordinated=false 时为空列表；跨边界的周期窗口会拆成两段"
        )
    )
    reason: str | None = Field(
        default=None, description="未协调原因；coordinated=true 时为 null"
    )


class CoordinationResponse(BaseModel):
    """响应体。

    - optimal：已证明权重总和最大，并在此前提下取到最小总偏移与字典序最小向量；
    - feasible_candidate：时限内找到可行候选（含偏移择优未完成证明）；
    - infeasible：已证明不存在能协调任何一条目标路线的偏移（offsets 为 null）；
    - unknown：时限内既未找到可行候选，也未能证明无解（offsets 为 null）。
    """

    status: CoordinationStatus
    cycle_length: int = Field(ge=1)
    offsets: list[int] | None = Field(
        description="各路口相对首个路口的偏移秒数，首个路口恒为 0；无解时为 null"
    )
    coordinated_weight: int = Field(ge=0, description="可连续通过路线的权重总和")
    routes: list[RouteCoordinationResult]


# ---------------------------------------------------------------------------
# 求解
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Window:
    """路口本地周期内的绿灯窗口（闭区间，不跨边界）。"""

    start: int
    end: int


@dataclass(frozen=True)
class _RouteData:
    id: str
    weight: int
    flow_ids: tuple[str, ...]
    windows: tuple[_Window, ...]
    # 第一个使“到达时刻跨度 > 绿灯长度”的路口下标；None 表示结构性可行。
    structural_block: int | None
    # structural_block 处的累计行驶时间极差（秒）。
    block_span: int


def _phase_windows(ix: CoordinationIntersection) -> dict[str, _Window]:
    """按相位顺序计算每条车流绿灯窗口在本地周期中的起止秒（闭区间）。"""
    cursor = 0
    windows: dict[str, _Window] = {}
    for p in sorted(ix.phases, key=lambda q: q.order):
        for fid in p.flows:
            windows[fid] = _Window(cursor, cursor + p.green - 1)
        cursor += p.green + p.all_red_after
    return windows


def _build_route_data(request: CoordinationRequest) -> list[_RouteData]:
    """提取每条路线在各路口的绿灯窗口，并做单路线结构性预检。

    到达路口 k 的时刻覆盖连续整数区间，跨度（极差）为
    span_k = Σ_{j<k} Δ_j；绿灯长 g_k。span_k > g_k - 1 时该路线在任何
    偏移下都无法协调（区间比绿灯还长，不可能整体落入窗口）。
    """
    maps = [_phase_windows(ix) for ix in request.intersections]
    deltas = [tt.max_seconds - tt.min_seconds for tt in request.travel_times]
    cycle = request.intersections[0].cycle_length

    routes: list[_RouteData] = []
    for r in request.routes:
        windows = tuple(maps[pos][fid] for pos, fid in enumerate(r.flows))
        span = 0
        block: int | None = None
        block_span = 0
        for k in range(1, len(r.flows)):
            span += deltas[k - 1]
            phase_green = windows[k].end - windows[k].start + 1
            # green_len == C（全周期常绿）时任意模值都是绿灯，不受限；
            # 否则到达极差 span 必须不超过绿灯长度减 1。
            if (
                block is None
                and phase_green < cycle
                and span > phase_green - 1
            ):
                block = k
                block_span = span
        routes.append(
            _RouteData(
                id=r.id,
                weight=r.weight,
                flow_ids=tuple(r.flows),
                windows=windows,
                structural_block=block,
                block_span=block_span,
            )
        )
    return routes


def _green_set(w: _Window, offset: int, cycle: int) -> frozenset[int]:
    g = w.end - w.start + 1
    return frozenset((w.start + offset + t) % cycle for t in range(g))


def _feasible_departure_seconds(
    route: _RouteData,
    offsets: Sequence[int],
    cycle: int,
    prefix_min: Sequence[int],
    prefix_span: Sequence[int],
) -> set[int] | None:
    """枚举使路线在给定偏移下连续通过的出发秒集合。

    返回 None 表示结构性不可能（到达跨度超过绿灯长度）；否则返回
    {d ∈ [0,C-1]：对每个路口、区间内每个整数到达时刻都落在绿灯窗口}。
    """
    if route.structural_block is not None:
        return None

    green_sets = [
        _green_set(w, offsets[k], cycle) for k, w in enumerate(route.windows)
    ]

    feasible: set[int] = set()
    for d in range(cycle):
        if d not in green_sets[0]:
            continue
        ok = True
        for k in range(1, len(route.windows)):
            first = d + prefix_min[k]
            for t in range(prefix_span[k] + 1):
                if (first + t) % cycle not in green_sets[k]:
                    ok = False
                    break
            if not ok:
                break
        if ok:
            feasible.add(d)
    return feasible


def _merge_windows(seconds: set[int]) -> list[list[int]]:
    """把排序后的出发秒合并为不跨周期边界的闭区间。"""
    if not seconds:
        return []
    out: list[list[int]] = []
    ordered = sorted(seconds)
    start = prev = ordered[0]
    for v in ordered[1:]:
        if v == prev + 1:
            prev = v
        else:
            out.append([start, prev])
            start = prev = v
    out.append([start, prev])
    return out


def _solve_cp(
    routes: Sequence[_RouteData],
    cycle: int,
    min_travel: Sequence[int],
    deltas: Sequence[int],
    time_limit_seconds: float,
) -> tuple[str, list[int] | None, int]:
    """分阶段 CP-SAT 求解，返回 (status, offsets, coordinated_weight)。

    字典序依次为：
      0. 可协调路线权重和 W 最大；
      1. 总偏移 Σo[k] 最小；
      2..K. o[1], o[2], … 依次最小（o[0] 恒为 0）。

    每个阶段证明最优后，把该阶段最优值作为等式锁进后续模型，保证
    字典序不被后续阶段破坏。
    """
    k_count = len(min_travel) + 1
    prefix_min = [0] * k_count
    prefix_span = [0] * k_count
    for k in range(1, k_count):
        prefix_min[k] = prefix_min[k - 1] + min_travel[k - 1]
        prefix_span[k] = prefix_span[k - 1] + deltas[k - 1]

    possible = [r for r in routes if r.structural_block is None]
    weight_expr = lambda p: sum(r.weight * p[r.id] for r in possible)

    def build(locked: dict[str, int], objective_key: str):
        """构建“偏移 + 路线可达”模型。

        locked 携带此前已确定的各层字典序最优值；objective_key 指定
        本阶段优化的目标（'weight' 为最大化，其余最小化）。
        """
        model = cp_model.CpModel()
        offsets = [model.new_constant(0)] + [
            model.new_int_var(0, cycle - 1, f"offset_{k}")
            for k in range(1, k_count)
        ]
        # 已确定的偏移坐标直接固定。
        for k in range(1, k_count):
            key = f"o{k}"
            if key in locked:
                model.add(offsets[k] == locked[key])

        passes: dict[str, cp_model.IntVar] = {}
        for r in possible:
            p = model.new_bool_var(f"pass_{r.id}")
            passes[r.id] = p
            d = model.new_int_var(0, cycle - 1, f"dep_{r.id}")

            # 路口 0：出发即到达，d 必须在本地绿灯窗口内（o[0]=0）。
            # 单相位全周期常绿（绿灯长 == C）时任意 d 均可。
            w0 = r.windows[0]
            if w0.end - w0.start + 1 < cycle:
                model.add(d >= w0.start).only_enforce_if(p)
                model.add(d <= w0.end).only_enforce_if(p)

            for k in range(1, k_count):
                span = prefix_span[k]
                w = r.windows[k]
                green_len = w.end - w.start + 1
                if green_len >= cycle:
                    # 全周期常绿：任意到达时刻都落在绿灯，无需偏移约束。
                    continue
                # 到达时刻覆盖连续整数区间 [d+L_k, d+L_k+span_k]
                # （L_k 为最小累计行驶时间，span_k 为各段极差累计）。
                arrive_min = d + prefix_min[k]
                arrive_max = arrive_min + span
                # 循环绿灯窗口：存在整数 m 使整段到达区间（含极差）
                # 落入 [s+o+mC, e+o+mC]。
                m_hi = (prefix_min[k] + cycle - 1) // cycle + 1
                m = model.new_int_var(-1, m_hi, f"m_{r.id}_{k}")
                model.add(
                    arrive_min >= w.start + offsets[k] + m * cycle
                ).only_enforce_if(p)
                model.add(
                    arrive_max <= w.end + offsets[k] + m * cycle
                ).only_enforce_if(p)

        wsum = weight_expr(passes)
        if "weight" in locked:
            # 锁定此前阶段确定的权重和，后续字典序阶段不得回退。
            model.add(wsum == locked["weight"])
        if "sum" in locked:
            model.add(sum(offsets[1:]) == locked["sum"])

        if objective_key == "weight":
            model.minimize(-wsum)
        elif objective_key == "sum":
            model.minimize(sum(offsets[1:]))
        else:
            idx = int(objective_key[1:])
            model.minimize(offsets[idx])
        return model, offsets, passes, wsum

    def run(model):
        solver = cp_model.CpSolver()
        remaining = max(0.05, deadline - monotonic())
        solver.parameters.max_time_in_seconds = min(
            max(0.05, time_limit_seconds), remaining
        )
        solver.parameters.num_search_workers = 8
        return solver, solver.solve(model)

    deadline = monotonic() + float(time_limit_seconds)

    # ---- 阶段 0：最大化权重和 ----
    model0, off_vars0, pass_vars, wsum0 = build({}, "weight")
    solver0, st0 = run(model0)

    if st0 == cp_model.INFEASIBLE:
        # 偏移域本身非空，理论上不会发生，防御性归为 unknown。
        return "unknown", None, 0
    if st0 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return "unknown", None, 0

    incumbent_weight = int(solver0.value(wsum0))
    proven_optimal = st0 == cp_model.OPTIMAL

    if st0 == cp_model.OPTIMAL and incumbent_weight == 0:
        # 已证明没有任何路线可以协调。
        return "infeasible", None, 0

    offsets = [0] + [
        int(solver0.value(off_vars0[k])) for k in range(1, k_count)
    ]

    # ---- 阶段 1..：逐层锁定并择优 ----
    locked: dict[str, int] = {"weight": incumbent_weight}
    refinement_proven = True

    def refine(objective_key: str, lock_key: str) -> bool:
        """在 locked 约束下最小化 objective_key 对应目标。

        证明最优则把最优值记入 locked[lock_key] 并返回 True；
        超时只更新候选偏移并返回 False。
        """
        nonlocal offsets
        model, off_vars, _, _ = build(locked, objective_key)
        solver, st = run(model)
        if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            offsets = [0] + [
                int(solver.value(off_vars[k])) for k in range(1, k_count)
            ]
            if st == cp_model.OPTIMAL:
                locked[lock_key] = int(solver.objective_value)
                return True
        return False

    # 先最小化总偏移，再依次最小化 o[1], o[2], ...；
    # 任一层未证明最优都只能标记为可行候选。
    if not refine("sum", "sum"):
        refinement_proven = False
    else:
        for k in range(1, k_count):
            if not refine(f"o{k}", f"o{k}"):
                refinement_proven = False
                break

    status = (
        "optimal" if proven_optimal and refinement_proven else "feasible_candidate"
    )
    return status, offsets, incumbent_weight


def coordinate(
    request: CoordinationRequest,
    time_limit_seconds: float = 10.0,
) -> CoordinationResponse:
    """协调求解主入口。"""
    cycle = request.intersections[0].cycle_length
    min_travel = [tt.min_seconds for tt in request.travel_times]
    deltas = [tt.max_seconds - tt.min_seconds for tt in request.travel_times]
    routes = _build_route_data(request)

    k_count = len(request.intersections)
    prefix_min = [0] * k_count
    prefix_span = [0] * k_count
    for k in range(1, k_count):
        prefix_min[k] = prefix_min[k - 1] + min_travel[k - 1]
        prefix_span[k] = prefix_span[k - 1] + deltas[k - 1]

    status, offsets, _weight = _solve_cp(
        routes, cycle, min_travel, deltas, time_limit_seconds
    )

    results: list[RouteCoordinationResult] = []
    if offsets is None:
        # infeasible / unknown：不返回偏移，所有路线均无通行窗口。
        for r in routes:
            results.append(
                RouteCoordinationResult(
                    id=r.id,
                    weight=r.weight,
                    coordinated=False,
                    departure_windows=[],
                    reason=_block_reason(r, request, status),
                )
            )
        return CoordinationResponse(
            status=status,
            cycle_length=cycle,
            offsets=None,
            coordinated_weight=0,
            routes=results,
        )

    total_weight = 0
    for r in routes:
        seconds = _feasible_departure_seconds(
            r, offsets, cycle, prefix_min, prefix_span
        )
        if seconds is None:
            coordinated = False
            windows: list[list[int]] = []
            reason: str | None = _block_reason(r, request, status)
        elif seconds:
            coordinated = True
            windows = _merge_windows(seconds)
            reason = None
            total_weight += r.weight
        else:
            coordinated = False
            windows = []
            reason = _empty_window_reason(
                r, offsets, cycle, prefix_min, prefix_span, request
            )
        results.append(
            RouteCoordinationResult(
                id=r.id,
                weight=r.weight,
                coordinated=coordinated,
                departure_windows=windows,
                reason=reason,
            )
        )

    return CoordinationResponse(
        status=status,
        cycle_length=cycle,
        offsets=offsets,
        coordinated_weight=total_weight,
        routes=results,
    )


def _ix_label(request: CoordinationRequest, k: int) -> str:
    return f"路口{k + 1}({request.intersections[k].id!r})"


def _block_reason(
    route: _RouteData, request: CoordinationRequest, status: str
) -> str | None:
    """结构性不可行的原因。"""
    k = route.structural_block
    if k is None:
        # 结构预检通过但求解器未给出可行偏移：按求解状态区分措辞。
        if status == "infeasible":
            return (
                "各路口绿灯窗口与行驶时间区间在周期内不存在统一偏移"
                "（该路线结构性可行，但与其他路线的偏移要求共同无解）"
            )
        return "求解时限内未能找到使该路线连续通过的偏移，也未能证明其无解"
    w = route.windows[k]
    green = w.end - w.start + 1
    return (
        f"到达{_ix_label(request, k)}车流 {route.flow_ids[k]!r} 的行驶时间极差"
        f"累计已达 {route.block_span} 秒，超过该路口绿灯长度 {green} 秒，"
        f"区间内的到达时刻不可能全部落在绿灯窗口，任何偏移都无法协调该路线"
    )


def _empty_window_reason(
    route: _RouteData,
    offsets: Sequence[int],
    cycle: int,
    prefix_min: Sequence[int],
    prefix_span: Sequence[int],
    request: CoordinationRequest,
) -> str:
    """结构性可行但在最终偏移下无统一出发秒：给出第一个无法满足的路口。"""
    green_sets = [
        _green_set(w, offsets[k], cycle) for k, w in enumerate(route.windows)
    ]

    common = set(range(cycle))
    blocked_k: int | None = None
    for k in range(len(route.windows)):
        allowed: set[int] = set()
        for d in range(cycle):
            if k == 0:
                ok = d in green_sets[0]
            else:
                first = d + prefix_min[k]
                span = prefix_span[k]
                ok = all(
                    (first + t) % cycle in green_sets[k]
                    for t in range(span + 1)
                )
            if ok:
                allowed.add(d)
        common &= allowed
        if not common:
            blocked_k = k
            break

    if blocked_k is None:
        return "在当前偏移下不存在统一出发秒使各路口均绿灯通过"
    return (
        "在当前最优偏移下出发秒无法同时满足全部路口：最先无法对齐的是"
        f"{_ix_label(request, blocked_k)}的车流 "
        f"{route.flow_ids[blocked_k]!r}绿灯窗口（偏移已优先让权重总和最大的"
        f"路线连续通过，该路线未被纳入）"
    )
