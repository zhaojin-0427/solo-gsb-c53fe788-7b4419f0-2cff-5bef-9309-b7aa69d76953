"""基于 OR-Tools CP-SAT 的路口信号周期编排求解。

决策内容（联合优化）：
1. 分组：每条车流恰好放入一个相位，同相位不得包含冲突车流；
2. 顺序：相位在周期中的环形排列（末相位之后回到首相位）；
3. 整数秒时长：各相位绿灯必须同时落在所有成员车流的绿灯区间内；
   相邻相位（含末->首）之间插入全红，时长不少于跨相位有向车流对
   （离开相位 -> 进入相位）清空时间的最大值；只有一个相位时全红为 0。

目标（字典序）：先最小化周期总秒数，再最小化相位数。

状态语义：
- optimal：CP-SAT 在给定上限内证明最优（含证明无解时的 infeasible）；
- feasible_candidate：时限内找到可行候选，但未证明最优；
- infeasible：已证明无解；
- unknown：时限内未找到可行解，也未能证明无解。
"""

from collections.abc import Mapping, Sequence

from ortools.sat.python import cp_model

from .models import Flow, Phase, Plan, PlanResponse

# 字典序权重：周期总秒数的权重必须大于相位数能贡献的最大差值。
# 周期每减少 1 秒（权重 CYCLE_WEIGHT）都优于相位任何变化（最多 n <= 10）。
_CYCLE_WEIGHT = 1000


def _parse_clearances(
    raw: Mapping[str, int], index: Mapping[str, int]
) -> dict[tuple[int, int], int]:
    out: dict[tuple[int, int], int] = {}
    for key, value in raw.items():
        a, b = key.split("->")
        ia, ib = index[a], index[b]
        if ia == ib:
            # 车流到自身的清空时间不参与跨相位计算，忽略。
            continue
        out[(ia, ib)] = max(out.get((ia, ib), 0), value)
    return out


def solve(
    flows: Sequence[Flow],
    conflict_pairs: Sequence[Sequence[str]],
    raw_clearances: Mapping[str, int],
    cycle_limit: int,
    time_limit_seconds: float = 10.0,
) -> PlanResponse:
    n = len(flows)
    index = {f.id: i for i, f in enumerate(flows)}
    clear = _parse_clearances(raw_clearances, index)

    conflicts: set[tuple[int, int]] = set()
    for pair in conflict_pairs:
        a, b = index[pair[0]], index[pair[1]]
        conflicts.add((min(a, b), max(a, b)))

    model = cp_model.CpModel()
    # rank 从 0（首相位）到 n-1；只有被使用的 rank 才存在相位。
    ranks = range(n)

    # m[i, r]: 车流 i 是否位于 rank r。
    m: dict[tuple[int, int], cp_model.IntVar] = {}
    for i in range(n):
        for r in ranks:
            m[i, r] = model.new_bool_var(f"m_{i}_{r}")
    # 每条车流恰在一个相位放行。
    for i in range(n):
        model.add_exactly_one(m[i, r] for r in ranks)

    # u[r]: rank r 是否被使用（存在相位）。
    u = [model.new_bool_var(f"u_{r}") for r in ranks]
    for r in ranks:
        model.add_max_equality(u[r], [m[i, r] for i in range(n)])
    # 使用的 rank 必须从 rank 0 连续排列（环形相位按使用顺序编号）。
    for r in range(1, n):
        model.add(u[r] <= u[r - 1])
    # 相位数 K = sum u[r]，且 1 <= K <= n。
    model.add(sum(u) >= 1)

    # 同相位不得含冲突车流：冲突的两条车流不能同在一个 rank。
    for a, b in conflicts:
        for r in ranks:
            model.add(m[a, r] + m[b, r] <= 1)

    # g[r]: rank r 的绿灯时长；必须同时不小于/不大于该 rank 上所有成员的区间。
    g = {}
    for r in ranks:
        g[r] = model.new_int_var(0, 60, f"g_{r}")
        for i, flow in enumerate(flows):
            model.add(g[r] >= flow.min_green).only_enforce_if(m[i, r])
            model.add(g[r] <= flow.max_green).only_enforce_if(m[i, r])
        model.add(g[r] == 0).only_enforce_if(u[r].Not())

    # co[r] = 1 当且仅当 u[r] 与 u[r+1] 同时使用（r -> r+1 存在相邻相位对）。
    co = {}
    for r in range(n - 1):
        co[r] = model.new_bool_var(f"co_{r}")
        model.add_min_equality(co[r], [u[r], u[r + 1]])

    # red[r]: rank r -> rank r+1 的全红；wrap 为末相位 -> 首相位的全红。
    red = {}
    for r in range(n - 1):
        red[r] = model.new_int_var(0, 10, f"red_{r}")
        model.add(red[r] == 0).only_enforce_if(co[r].Not())
    wrap = model.new_int_var(0, 10, "wrap")

    # z = 1 当且仅当 K >= 2，此时存在末->首的环形过渡。
    z = model.new_bool_var("wrap_active")
    model.add(z == 0).only_enforce_if(u[1].Not())
    model.add(z == 1).only_enforce_if(u[1])
    model.add(wrap == 0).only_enforce_if(z.Not())

    # is_last[r] = 1 当且仅当 r 是最后一个被使用的 rank（K>=2 时即末相位）。
    is_last = {}
    for r in range(1, n):
        is_last[r] = model.new_bool_var(f"last_{r}")
        if r + 1 < n:
            model.add(is_last[r] == 1).only_enforce_if(u[r], u[r + 1].Not())
            model.add(is_last[r] == 0).only_enforce_if(u[r].Not())
            model.add(is_last[r] == 0).only_enforce_if(u[r + 1])
    model.add(is_last[n - 1] == u[n - 1])
    # K>=2 时恰有一个 is_last；K==1 时全部为 0（is_last 仅在 u[r]=1 且非首相位时取 1）。
    model.add(sum(is_last.values()) == z)

    def add_red_pair_lower_bounds(
        red_var: cp_model.IntVar,
        from_rank: int,
        to_rank: int,
    ) -> None:
        """当 i 位于 from_rank 且 j 位于 to_rank 时，施加 red_var >= clear(i->j)。

        过渡是否存在、以及不存在时 red_var == 0，由调用方统一处理。
        """
        for (i, j), value in clear.items():
            if value <= 0:
                continue
            active = model.new_bool_var(f"pair_{i}_{j}_{from_rank}_{to_rank}")
            model.add_bool_and(m[i, from_rank], m[j, to_rank]).only_enforce_if(active)
            model.add_bool_or(
                [m[i, from_rank].Not(), m[j, to_rank].Not()]
            ).only_enforce_if(active.Not())
            model.add(red_var >= value).only_enforce_if(active)

    for r in range(n - 1):
        add_red_pair_lower_bounds(red[r], r, r + 1)

    for r in range(1, n):
        # 末 rank r -> 首 rank 0 的环形清空约束，仅在 r 确实为最后一个使用 rank 时生效。
        for (i, j), value in clear.items():
            if value <= 0:
                continue
            active = model.new_bool_var(f"pair_wrap_{i}_{j}_{r}")
            model.add_bool_and(is_last[r], m[i, r], m[j, 0]).only_enforce_if(active)
            model.add_bool_or(
                [is_last[r].Not(), m[i, r].Not(), m[j, 0].Not()]
            ).only_enforce_if(active.Not())
            model.add(wrap >= value).only_enforce_if(active)

    # 周期总秒数：被使用相位的绿灯 + 相邻相位间的全红（含末->首）。
    cycle = model.new_int_var(1, max(1, cycle_limit), "cycle")
    model.add(
        cycle
        == sum(g[r] for r in ranks)
        + sum(red[r] for r in range(n - 1))
        + wrap
    )
    model.add(cycle <= cycle_limit)

    # 先最小化周期总秒数，再最小化相位数。
    model.minimize(_CYCLE_WEIGHT * cycle + sum(u))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.num_search_workers = 8
    status = solver.solve(model)

    if status == cp_model.INFEASIBLE:
        return PlanResponse(status="infeasible", plan=None)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return PlanResponse(status="unknown", plan=None)

    used_ranks = [r for r in ranks if solver.value(u[r]) == 1]
    phases: list[Phase] = []
    for order, r in enumerate(used_ranks, start=1):
        members = [
            flows[i].id
            for i in range(n)
            if solver.value(m[i, r]) == 1
        ]
        all_red = (
            solver.value(wrap) if r == used_ranks[-1] and len(used_ranks) >= 2
            else (solver.value(red[r]) if r < n - 1 else 0)
        )
        phases.append(
            Phase(
                order=order,
                flows=members,
                green=int(solver.value(g[r])),
                all_red_after=int(all_red),
            )
        )

    cycle_length = int(solver.value(cycle))
    plan = Plan(
        phases=phases,
        cycle_length=cycle_length,
        phase_count=len(phases),
    )
    resp_status = "optimal" if status == cp_model.OPTIMAL else "feasible_candidate"
    return PlanResponse(status=resp_status, plan=plan)
