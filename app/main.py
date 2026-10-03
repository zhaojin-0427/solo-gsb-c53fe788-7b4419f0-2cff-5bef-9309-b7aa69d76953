"""FastAPI 应用入口。"""

import os

from fastapi import FastAPI

from .coordination import CoordinationRequest, CoordinationResponse, coordinate
from .models import PlanRequest, PlanResponse
from .solver import solve

SOLVER_TIME_LIMIT_SECONDS = float(
    os.environ.get("SOLVER_TIME_LIMIT_SECONDS", "10")
)

app = FastAPI(
    title="路口信号周期编排与多路口绿波协调服务",
    description=(
        "POST /plans：给定车流绿灯区间、无向冲突对与有向清空时间，联合决定"
        "相位分组、环形顺序与整数秒时长，在周期上限内先最小化周期总秒数、"
        "再最小化相位数。\n\n"
        "POST /coordination：给定 2~8 个同周期的已生成路口方案、相邻路口"
        "行驶时间闭区间与带权目标路线，计算各路口相对偏移使连续通过路线的"
        "权重总和最大（其次总偏移最小、偏移向量字典序最小）。"
    ),
    version="1.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/plans", response_model=PlanResponse)
def create_plan(request: PlanRequest) -> PlanResponse:
    """编排一个信号周期方案。非法输入由 FastAPI/Pydantic 返回 422。"""
    return solve(
        flows=request.flows,
        conflict_pairs=request.conflicts,
        raw_clearances=request.clearances,
        cycle_limit=request.cycle_limit,
        time_limit_seconds=SOLVER_TIME_LIMIT_SECONDS,
    )


@app.post("/coordination", response_model=CoordinationResponse)
def create_coordination(request: CoordinationRequest) -> CoordinationResponse:
    """多路口绿波协调。相位/周期/车流引用不一致等非法输入返回 422。"""
    return coordinate(
        request=request,
        time_limit_seconds=SOLVER_TIME_LIMIT_SECONDS,
    )
