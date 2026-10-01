"""FastAPI 应用入口。"""

import os

from fastapi import FastAPI

from .models import PlanRequest, PlanResponse
from .solver import solve

SOLVER_TIME_LIMIT_SECONDS = float(
    os.environ.get("SOLVER_TIME_LIMIT_SECONDS", "10")
)

app = FastAPI(
    title="路口信号周期编排服务",
    description=(
        "给定车流绿灯区间、无向冲突对与有向清空时间，联合决定相位分组、"
        "环形顺序与整数秒时长，在周期上限内先最小化周期总秒数、再最小化相位数。"
    ),
    version="1.0.0",
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
