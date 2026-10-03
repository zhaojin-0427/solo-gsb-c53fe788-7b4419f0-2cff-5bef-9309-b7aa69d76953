"""FastAPI 应用入口。"""

import os

from fastapi import FastAPI

from .coordination import coordinate
from .models import CoordinationRequest, CoordinationResponse, PlanRequest, PlanResponse
from .solver import solve

SOLVER_TIME_LIMIT_SECONDS = float(
    os.environ.get("SOLVER_TIME_LIMIT_SECONDS", "10")
)

app = FastAPI(
    title="路口信号周期编排与绿波协调服务",
    description=(
        "支持单路口信号周期编排，以及多路口同周期方案的绿波相对偏移协调。"
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
    """协调多个同周期路口的相对绿波偏移。非法输入返回 422。"""
    return coordinate(
        plans=request.plans,
        travel_intervals=request.travel_intervals,
        routes=request.routes,
        time_limit_seconds=SOLVER_TIME_LIMIT_SECONDS,
    )
