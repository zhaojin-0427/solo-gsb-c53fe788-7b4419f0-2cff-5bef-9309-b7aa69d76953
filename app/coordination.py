"""多路口绿波协调的 CP-SAT 求解。"""

from collections.abc import Sequence
import time
from typing import Literal, Optional

from ortools.sat.python import cp_model

from .models import (
    CoordinationResponse,
    CoordinationRoute,
    DepartureWindow,
    Plan,
    RouteCoordinationResult,
    TravelInterval,
)


class _RouteMeta:
    """预计算一条路线的循环圆弧与可协调性。"""

    def __init__(
        self,
        route_id: str,
        route: CoordinationRoute,
        plans: Sequence[Plan],
        travel_intervals: Sequence[TravelInterval],
        cycle: int,
    ) -> None:
        self.id = route_id
        self.route = route
        self.weight = route.weight
        self.possible = True
        self.reason: Optional[str] = None
        self.arcs: list[tuple[int, int]] = []

        flow_maps = []
        for plan in plans:
            starts: dict[str, int] = {}
            cursor = 0
            for phase in plan.phases:
                for flow_id in phase.flows:
                    starts[flow_id] = cursor
                cursor += phase.green + phase.all_red_after
            flow_maps.append(starts)

        min_cumulative = 0
        max_cumulative = 0
        for intersection_index, flow_id in enumerate(route.flow_ids):
            phase = next(
                phase
                for phase in plans[intersection_index].phases
                if flow_id in phase.flows
            )
            green_start = flow_maps[intersection_index][flow_id]
            span = max_cumulative - min_cumulative
            # 对闭区间内所有行驶时间都放行：整数出发窗口长度为 green-span。
            window_length = phase.green - span
            if window_length <= 0:
                self.possible = False
                self.reason = "green_window_too_short"
                break

            start = (
                green_start - min_cumulative
            ) % cycle
            self.arcs.append((start, window_length))

            if intersection_index < len(travel_intervals):
                min_cumulative += travel_intervals[intersection_index].min_travel
                max_cumulative += travel_intervals[intersection_index].max_travel


def _arc_to_intervals(start: int, length: int, cycle: int) -> list[tuple[int, int]]:
    """把循环圆弧转换成 0..C-1 上的普通闭区间。"""
    if length == cycle:
        return [(0, cycle - 1)]
    if start + length <= cycle:
        return [(start, start + length - 1)]
    return [(0, (start + length) % cycle - 1), (start, cycle - 1)]


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _intersect_arcs(
    arcs: Sequence[tuple[int, int]], cycle: int
) -> list[tuple[int, int, bool]]:
    """求多个循环圆弧交集，返回 (start, end, wraps) 闭窗口。"""
    current = _arc_to_intervals(arcs[0][0], arcs[0][1], cycle)
    for start, length in arcs[1:]:
        pieces = _arc_to_intervals(start, length, cycle)
        intersections: list[tuple[int, int]] = []
        for left_start, left_end in current:
            for right_start, right_end in pieces:
                begin = max(left_start, right_start)
                finish = min(left_end, right_end)
                if begin <= finish:
                    intersections.append((begin, finish))
        current = _merge_intervals(intersections)
        if not current:
            return []

    if len(current) >= 2 and current[0][0] == 0 and current[-1][1] == cycle - 1:
        windows = [(current[-1][0], current[0][1], True)]
        windows.extend(
            (start, end, False) for start, end in current[1:-1]
        )
        return windows
    return [(start, end, False) for start, end in current]


def _build_model(
    cycle: int,
    route_metas: Sequence[_RouteMeta],
    required_weight: Optional[tuple[str, int]] = None,
    fixed_total_offset: Optional[int] = None,
    fixed_offsets: Optional[dict[int, int]] = None,
    minimize_offset_index: Optional[int] = None,
    objective: Literal["weight", "offset"] = "offset",
) -> tuple[cp_model.CpModel, dict[str, object]]:
    model = cp_model.CpModel()
    intersection_count = len(route_metas[0].route.flow_ids)
    offsets = [
        model.new_constant(0)
        if i == 0
        else model.new_int_var(0, cycle - 1, f"offset_{i}")
        for i in range(intersection_count)
    ]

    selected_vars = []
    variables = {"offsets": offsets, "selected": {}}

    for meta in route_metas:
        if not meta.possible:
            continue
        x = model.new_bool_var(f"route_{meta.id}")
        selected_vars.append(meta.weight * x)
        variables["selected"][meta.id] = x
        departure = model.new_int_var(0, cycle - 1, f"departure_{meta.id}")

        for intersection_index, (start, length) in enumerate(meta.arcs):
            # local_arrival = departure - start - offset + k*C；
            # d/start/offset 均在 [0,C)，k=0/1/2 可覆盖循环归一化。
            raw = departure - start - offsets[intersection_index]
            entry = model.new_bool_var(f"entry_{meta.id}_{intersection_index}")
            model.add(entry == x)
            arc_branches = []

            for wrap_index in (0, 1, 2):
                branch = model.new_bool_var(
                    f"arc_branch_{meta.id}_{intersection_index}_{wrap_index}"
                )
                model.add(branch <= entry)
                shifted = raw + wrap_index * cycle
                model.add(shifted >= 0).only_enforce_if(branch)
                model.add(shifted <= length - 1).only_enforce_if(branch)
                arc_branches.append(branch)

            model.add(sum(arc_branches) == entry)

        # x=1 等价于该统一出发时刻在所有路口均命中绿灯。

    weight_expr = sum(selected_vars)
    if required_weight is not None:
        comparator, value = required_weight
        if comparator == "==":
            model.add(weight_expr == value)
        elif comparator == ">=":
            model.add(weight_expr >= value)
        else:
            raise ValueError(f"未知权重比较符：{comparator}")

    total_expr = sum(offsets)
    if fixed_total_offset is not None:
        model.add(total_expr == fixed_total_offset)

    fixed_offsets = fixed_offsets or {}
    for index, value in fixed_offsets.items():
        model.add(offsets[index] == value)

    if objective == "weight":
        model.maximize(weight_expr)
    elif minimize_offset_index is None:
        model.minimize(total_expr)
    else:
        model.minimize(offsets[minimize_offset_index])

    variables["weight_expr"] = weight_expr
    variables["total_expr"] = total_expr
    return model, variables


def _run_stage(
    cycle: int,
    route_metas: Sequence[_RouteMeta],
    deadline: float,
    required_weight: Optional[tuple[str, int]] = None,
    fixed_total_offset: Optional[int] = None,
    fixed_offsets: Optional[dict[int, int]] = None,
    minimize_offset_index: Optional[int] = None,
    objective: Literal["weight", "offset"] = "offset",
) -> tuple[int, Optional[dict[str, object]], list[int]]:
    model, variables = _build_model(
        cycle=cycle,
        route_metas=route_metas,
        required_weight=required_weight,
        fixed_total_offset=fixed_total_offset,
        fixed_offsets=fixed_offsets,
        minimize_offset_index=minimize_offset_index,
        objective=objective,
    )
    solver = cp_model.CpSolver()
    remaining = max(0.01, deadline - time.monotonic())
    solver.parameters.max_time_in_seconds = remaining
    solver.parameters.num_search_workers = 8
    status = solver.solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return status, None, []

    offsets_vars = variables["offsets"]
    assert isinstance(offsets_vars, list)
    offsets = [int(solver.value(var)) for var in offsets_vars]
    selected_vars = variables["selected"]
    assert isinstance(selected_vars, dict)
    selected = {
        route_id: solver.value(var) == 1
        for route_id, var in selected_vars.items()
    }
    return status, {"selected": selected}, offsets


def _route_results(
    route_metas: Sequence[_RouteMeta],
    offsets: Optional[Sequence[int]],
    cycle: int,
    incomplete: bool = False,
) -> tuple[list[RouteCoordinationResult], int]:
    results: list[RouteCoordinationResult] = []
    coordinated_weight = 0

    for meta in route_metas:
        windows: list[DepartureWindow] = []
        coordinated = False
        reason: Optional[str] = None

        if not meta.possible:
            reason = meta.reason
        elif incomplete or offsets is None:
            reason = "solver_incomplete"
        else:
            shifted_arcs = [
                ((start + offsets[index]) % cycle, length)
                for index, (start, length) in enumerate(meta.arcs)
            ]
            windows = [
                DepartureWindow(start=start, end=end, wraps=wraps)
                for start, end, wraps in _intersect_arcs(shifted_arcs, cycle)
            ]
            coordinated = bool(windows)
            if coordinated:
                coordinated_weight += meta.weight
            else:
                reason = "no_compatible_offset"

        results.append(
            RouteCoordinationResult(
                id=meta.id,
                weight=meta.weight,
                coordinated=coordinated,
                departure_windows=windows,
                reason=reason,
            )
        )

    return results, coordinated_weight


def coordinate(
    plans: Sequence[Plan],
    travel_intervals: Sequence[TravelInterval],
    routes: Sequence[CoordinationRoute],
    time_limit_seconds: float = 10.0,
) -> CoordinationResponse:
    """求解绿波偏移，并按权重、总偏移、偏移字典序依次优化。"""
    cycle = plans[0].cycle_length
    metas: list[_RouteMeta] = []
    for index, route in enumerate(routes):
        route_id = route.id or f"R{index + 1}"
        metas.append(
            _RouteMeta(route_id, route, plans, travel_intervals, cycle)
        )

    possible_weight = sum(meta.weight for meta in metas if meta.possible)
    if possible_weight == 0:
        zero_offsets = [0] * len(plans)
        results, coordinated_weight = _route_results(metas, zero_offsets, cycle)
        return CoordinationResponse(
            status="infeasible",
            cycle_length=cycle,
            offsets=zero_offsets,
            coordinated_weight=coordinated_weight,
            routes=results,
        )

    deadline = time.monotonic() + float(time_limit_seconds)
    first_status, first_solution, first_offsets = _run_stage(
        cycle=cycle,
        route_metas=metas,
        deadline=deadline,
        minimize_offset_index=None,
        objective="weight",
    )

    if first_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        results, _ = _route_results(metas, None, cycle, incomplete=True)
        return CoordinationResponse(
            status="unknown",
            cycle_length=cycle,
            offsets=None,
            coordinated_weight=0,
            routes=results,
        )

    assert first_solution is not None
    first_selected = first_solution["selected"]
    assert isinstance(first_selected, dict)
    candidate_weight = sum(
        meta.weight
        for meta in metas
        if first_selected.get(meta.id, False)
    )
    candidate_offsets = first_offsets
    proven = first_status == cp_model.OPTIMAL
    weight_constraint: tuple[str, int] = (
        ("==", candidate_weight) if proven else (">=", candidate_weight)
    )

    total_status, total_solution, total_offsets = _run_stage(
        cycle=cycle,
        route_metas=metas,
        deadline=deadline,
        required_weight=weight_constraint,
        minimize_offset_index=None,
    )
    if (
        total_status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        and total_solution is not None
    ):
        candidate_offsets = total_offsets
        proven = proven and total_status == cp_model.OPTIMAL
        total_selected = total_solution["selected"]
        assert isinstance(total_selected, dict)
        candidate_weight = sum(
            meta.weight
            for meta in metas
            if total_selected.get(meta.id, False)
        )
        # 后续字典序阶段固定该候选实际协调权重，避免用权重交换偏移。
        weight_constraint = ("==", candidate_weight)
    elif total_status == cp_model.INFEASIBLE:
        # 理论上不会发生：保留第一阶段候选，标记为未证明候选。
        proven = False
    else:
        proven = False

    fixed_offsets: dict[int, int] = {}
    total_offset = sum(candidate_offsets)

    for index in range(1, len(plans)):
        status, _, offsets = _run_stage(
            cycle=cycle,
            route_metas=metas,
            deadline=deadline,
            required_weight=weight_constraint,
            fixed_total_offset=total_offset,
            fixed_offsets=fixed_offsets,
            minimize_offset_index=index,
        )
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            candidate_offsets = offsets
            proven = proven and status == cp_model.OPTIMAL
            fixed_offsets[index] = offsets[index]
        else:
            # 未能继续完成字典序证明时，保留当前候选并标记为可行候选。
            proven = False
            break

    results, coordinated_weight = _route_results(metas, candidate_offsets, cycle)
    return CoordinationResponse(
        status="optimal" if proven else "feasible_candidate",
        cycle_length=cycle,
        offsets=candidate_offsets,
        coordinated_weight=coordinated_weight,
        routes=results,
    )
