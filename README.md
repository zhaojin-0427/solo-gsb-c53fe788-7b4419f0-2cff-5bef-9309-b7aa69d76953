# 路口信号周期编排服务

给定 2～10 条有向车流、各自可接受的绿灯秒数区间、无向冲突对、有向清空时间与周期上限，
用 [OR-Tools CP-SAT](https://developers.google.com/optimization/cp/cp_solver) 联合决定：

1. **分组**：每条车流恰好放入一个相位放行；同相位不得包含相互冲突的车流；
2. **顺序**：相位在周期中的环形排列（末相位之后回到首相位）；
3. **整数秒时长**：相位绿灯必须同时落在所有成员车流的绿灯区间内；
   相邻相位（含末相位 → 首相位）之间插入**全红**，时长不少于所有跨相位
   有向车流对（离开相位车流 → 进入相位车流）清空时间的最大值；
   整个周期只有一个相位时全红为 0。

优化目标为字典序：**先最小化周期总秒数，周期并列时再最小化相位数**。

技术栈：Python 3.12 · FastAPI · OR-Tools（CP-SAT）· Docker Compose。

## 一键启动

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`。

- 健康检查：`GET http://localhost:8000/health`
- 编排接口：`POST http://localhost:8000/plans`
- 多路口绿波协调：`POST http://localhost:8000/coordination`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`
- OpenAPI Schema：`http://localhost:8000/openapi.json`

可用环境变量 `SOLVER_TIME_LIMIT_SECONDS`（默认 `10` 秒）调整单次求解的时间上限
（`docker-compose.yml` 中已列出）。

本地不使用 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 请求格式 `POST /plans`

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `flows` | 数组，2～10 项 | 车流列表，`id` 为非空字符串且全局唯一 |
| `flows[].min_green` / `max_green` | 整数 | 各自取值 1～60 秒，且 `min_green <= max_green` |
| `conflicts` | 字符串二元组数组 | 无向冲突对（`[a,b]` 与 `[b,a]` 等价，重复报错）；两个 ID 必须存在且互不相同 |
| `clearances` | 对象 | 键为 `"id_a->id_b"` 的有向清空时间，值 0～10 秒；**未列出的有向对按 0 处理** |
| `cycle_limit` | 正整数 | 周期总秒数上限 |

任何非法 ID、区间或冲突对都会返回 **HTTP 422**（响应体 `detail` 中给出原因）。

## 响应格式

```json
{
  "status": "optimal",
  "plan": {
    "phases": [
      {"order": 1, "flows": ["B"], "green": 15, "all_red_after": 3},
      {"order": 2, "flows": ["A", "C"], "green": 20, "all_red_after": 5}
    ],
    "cycle_length": 43,
    "phase_count": 2
  }
}
```

- `phases[].order`：相位顺序，从 1 开始，环形周期中末相位之后回到相位 1；
- `phases[].flows`：本相位放行的车流；
- `phases[].green`：本相位绿灯秒数，落在所有成员车流区间的交集内；
- `phases[].all_red_after`：本相位之后、下一相位之前的全红秒数
  （末相位给出的是末相位 → 首相位的全红；单相位时为 0）；
- `cycle_length`：周期总秒数 = 各相位绿灯之和 + 各段全红之和；
- `phase_count`：相位数。

`status` 取值：

| status | 含义 |
| --- | --- |
| `optimal` | 已证明在周期上限内达到最短周期（并列最短时相位数最少），此时方可标记为最优 |
| `feasible_candidate` | 时限内找到一个可行候选方案，但未能证明其最优 |
| `infeasible` | 已证明周期上限内无解，此时 `plan` 为 `null` |
| `unknown` | 时限内既未找到可行候选，也未能证明无解，此时 `plan` 为 `null` |

## 请求 / 响应示例

以下示例均为对实际运行服务的真实调用与输出。

### 1. 最优方案（optimal）

三条车流 A、B、C，B 分别与 A、C 冲突，因此 B 必须独占相位；A、C 不冲突可合组，
合组绿灯取共同可接受值。

请求：

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "flows": [
      {"id": "A", "min_green": 10, "max_green": 30},
      {"id": "B", "min_green": 15, "max_green": 30},
      {"id": "C", "min_green": 20, "max_green": 25}
    ],
    "conflicts": [["A", "B"], ["B", "C"]],
    "clearances": {
      "A->B": 3, "B->A": 2,
      "A->C": 4, "C->A": 1,
      "B->C": 3, "C->B": 5
    },
    "cycle_limit": 120
  }'
```

实际响应（格式化后）：

```json
{
  "status": "optimal",
  "plan": {
    "phases": [
      { "order": 1, "flows": ["B"],      "green": 15, "all_red_after": 3 },
      { "order": 2, "flows": ["A", "C"], "green": 20, "all_red_after": 5 }
    ],
    "cycle_length": 43,
    "phase_count": 2
  }
}
```

校验：绿灯 15+20 = 35 秒；相位 1→2 的全红取 B→A/B→C 清空时间最大值
`max(2, 3) = 3`；末相位 2→首相位 1 的全红取 A→B、C→B 的最大值 `max(3, 5) = 5`；
周期 15+3+20+5 = **43 秒**。

### 2. 证明无解（infeasible）

两条冲突车流各需至少 30 秒绿灯，且双向各 4 秒清空，周期至少 68 秒，
上限只给 50 秒：

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "flows": [
      {"id": "A", "min_green": 30, "max_green": 60},
      {"id": "B", "min_green": 30, "max_green": 60}
    ],
    "conflicts": [["A", "B"]],
    "clearances": {"A->B": 4, "B->A": 4},
    "cycle_limit": 50
  }'
```

实际响应：

```json
{ "status": "infeasible", "plan": null }
```

把 `cycle_limit` 改为 `68` 即可得到 `optimal`、周期恰为 68 秒的方案。

### 3. 可行候选但未证明最优（feasible_candidate）

当实例规模较大、搜索空间在 `SOLVER_TIME_LIMIT_SECONDS`（默认 10 秒）内无法完成
最优性证明（但已找到可行方案）时，响应明确区分于已证明最优。仓库内置了一个
10 条车流两两完全冲突、90 个有向清空时间的硬实例（`examples/hard_10_flows.json`）：

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  --data @examples/hard_10_flows.json
```

在默认 10 秒时限下对运行中服务的实际响应：

```json
{
  "status": "feasible_candidate",
  "plan": {
    "phases": [
      { "order": 1, "flows": ["F6"], "green": 7, "all_red_after": 1 },
      { "order": 2, "flows": ["F2"], "green": 2, "all_red_after": 0 },
      { "order": 3, "flows": ["F3"], "green": 5, "all_red_after": 3 },
      { "order": 4, "flows": ["F1"], "green": 3, "all_red_after": 1 },
      { "order": 5, "flows": ["F0"], "green": 2, "all_red_after": 3 },
      { "order": 6, "flows": ["F9"], "green": 5, "all_red_after": 0 },
      { "order": 7, "flows": ["F7"], "green": 7, "all_red_after": 1 },
      { "order": 8, "flows": ["F4"], "green": 6, "all_red_after": 2 },
      { "order": 9, "flows": ["F5"], "green": 7, "all_red_after": 4 },
      { "order": 10, "flows": ["F8"], "green": 1, "all_red_after": 0 }
    ],
    "cycle_length": 60,
    "phase_count": 10
  }
}
```

因 CP-SAT 并行搜索带随机性，未证最优时具体相位顺序/周期可能随机器与运行次数
略有差异（`status` 一致）；调大 `SOLVER_TIME_LIMIT_SECONDS` 后重新请求，
求解器可能把状态推进为 `optimal`。

### 4. 非法输入（HTTP 422）

例如车流 B 的绿灯下限大于上限：

```bash
curl -i -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "flows": [
      {"id": "A", "min_green": 10, "max_green": 30},
      {"id": "B", "min_green": 15, "max_green": 10}
    ],
    "conflicts": [],
    "clearances": {},
    "cycle_limit": 120
  }'
```

实际响应（节选）：

```
HTTP/1.1 422 Unprocessable Content
```

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body"],
      "msg": "Value error, 车流 B 的 min_green(15) 不能大于 max_green(10)",
      "input": {
        "flows": [
          {"id": "A", "min_green": 10, "max_green": 30},
          {"id": "B", "min_green": 15, "max_green": 10}
        ],
        "conflicts": [],
        "clearances": {},
        "cycle_limit": 120
      },
      "ctx": { "error": {} }
    }
  ]
}
```

冲突对/清空键引用不存在的 ID、车流 ID 重复、绿值超出 1～60、清空值超出 0～10、
车流连数不在 2～10、`cycle_limit` 非正整数等情况同样返回 422。

## 多路口绿波协调 `POST /coordination`

接口接收 2～8 个**按行驶方向排列**的已生成路口方案。所有路口必须使用相同周期；
首个路口相对偏移固定为 `0`，其余路口的偏移为 `0 ~ cycle_length-1` 秒。

相邻路口的行驶时间以整数闭区间给出：对路线上每一段，区间内**每个整数行驶时间**
对应的到达时刻都必须落在该路口对应车流的绿灯窗口内（即带行驶时间不确定性的鲁棒绿波）。
目标路线用每个路口一个车流 ID 表示，并通过正整数 `weight` 指定优先级。

优化顺序为：

1. 最大化可连续通过路线的权重总和；
2. 权重相同则最小化所有偏移之和；
3. 仍相同则最小化偏移向量（首个元素固定为 0）的字典序。

### 请求字段

| 字段 | 规则 |
| --- | --- |
| `plans` | 2～8 个路口方案，结构与 `/plans` 返回的 `plan` 相同 |
| `travel_intervals` | 相邻路口区间，长度必须为 `plans.length - 1`；`min_travel <= max_travel`，均为非负整数 |
| `routes` | 至少一条路线；`flow_ids` 长度必须等于路口数，且每个 ID 必须能在对应路口方案中找到 |
| `routes[].id` | 可选路线 ID；提供时必须唯一，省略时自动使用 `R1`、`R2`... |
| `routes[].weight` | 1～1,000,000,000 的正整数权重 |

输入还会校验：各方案周期一致、`phase_count` 与 `phases` 数量一致、`phase.order`
从 1 连续排列、同一车流不能重复出现在多个相位、每相位绿灯与之后全红之和等于周期。
任何不一致都返回 **HTTP 422**。

### 响应字段

- `status`：
  - `optimal`：已证明按“权重总和、总偏移、偏移字典序”得到最优；
  - `feasible_candidate`：找到可行偏移，但求解时限内未证明最优；
  - `infeasible`：所有路线在给定绿灯/行驶区间下都没有任何可通行出发秒；
  - `unknown`：求解时限内未找到候选，也未能证明无解。
- `offsets`：各路口相对偏移，首个值固定为 0。
- `coordinated_weight`：最终偏移下可连续通过路线的权重总和。
- `routes[].departure_windows`：相对首个路口周期开始时间的统一出发秒窗口；
  窗口端点均含秒，`wraps: true` 表示窗口由 `start..C-1` 与 `0..end` 两段组成。
- `routes[].reason`：
  - `null`：该路线已协调；
  - `no_compatible_offset`：单路线本身可协调，但为最大化总权重选择的偏移下未连续通过；
  - `green_window_too_short`：某段行驶时间不确定性跨度不小于该路线绿灯，没有任何偏移可协调；
  - `solver_incomplete`：求解器超时且没有该路线的候选结果。

### 协调示例（含一条被高权重路线挤出的路线）

两个路口均为 60 秒周期，每段绿灯 10 秒、全红 10 秒。东向直行路线权重 10；
第一条路口东向到达后、第二条路口转 X 相位的路线权重 1。相邻路口行驶时间为 1～2 秒。
求解器保留权重 10 的主路线，总偏移最小的向量是 `[0, 0]`。

请求：

```bash
curl -s -X POST http://localhost:8000/coordination \
  -H 'Content-Type: application/json' \
  -d '{
    "plans": [
      {
        "cycle_length": 60,
        "phase_count": 3,
        "phases": [
          {"order": 1, "flows": ["E"], "green": 10, "all_red_after": 10},
          {"order": 2, "flows": ["X"], "green": 10, "all_red_after": 10},
          {"order": 3, "flows": ["N"], "green": 10, "all_red_after": 10}
        ]
      },
      {
        "cycle_length": 60,
        "phase_count": 3,
        "phases": [
          {"order": 1, "flows": ["E"], "green": 10, "all_red_after": 10},
          {"order": 2, "flows": ["X"], "green": 10, "all_red_after": 10},
          {"order": 3, "flows": ["N"], "green": 10, "all_red_after": 10}
        ]
      }
    ],
    "travel_intervals": [{"min_travel": 1, "max_travel": 2}],
    "routes": [
      {"id": "main-eastbound", "flow_ids": ["E", "E"], "weight": 10},
      {"id": "turn-at-next", "flow_ids": ["E", "X"], "weight": 1}
    ]
  }'
```

实际响应（格式化后）：

```json
{
  "status": "optimal",
  "cycle_length": 60,
  "offsets": [0, 0],
  "coordinated_weight": 10,
  "routes": [
    {
      "id": "main-eastbound",
      "weight": 10,
      "coordinated": true,
      "departure_windows": [
        {"start": 0, "end": 7, "wraps": false}
      ],
      "reason": null
    },
    {
      "id": "turn-at-next",
      "weight": 1,
      "coordinated": false,
      "departure_windows": [],
      "reason": "no_compatible_offset"
    }
  ]
}
```

校验：主路线在出发秒 `0..7` 内任取一个统一出发时间，经过 1 或 2 秒行驶，
第二路口的到达时间都处于东向绿灯 `0..9`；转向路线若要在第二路口进入 X 绿灯
（20～29 秒）会与主路线要求的偏移冲突，因此在权重最大化后未协调。

### 无解示例（绿灯窗口无法覆盖行驶时间跨度）

同样两个路口，行驶时间区间为 `0..20`。该区间跨度为 21 个整数秒，而东向绿灯只有
10 秒；无论偏移如何，都不可能让 0～20 秒内的每个到达时刻都落在绿灯中：

```bash
curl -s -X POST http://localhost:8000/coordination \
  -H 'Content-Type: application/json' \
  -d '{
    "plans": [
      {
        "cycle_length": 60,
        "phase_count": 3,
        "phases": [
          {"order": 1, "flows": ["E"], "green": 10, "all_red_after": 10},
          {"order": 2, "flows": ["X"], "green": 10, "all_red_after": 10},
          {"order": 3, "flows": ["N"], "green": 10, "all_red_after": 10}
        ]
      },
      {
        "cycle_length": 60,
        "phase_count": 3,
        "phases": [
          {"order": 1, "flows": ["E"], "green": 10, "all_red_after": 10},
          {"order": 2, "flows": ["X"], "green": 10, "all_red_after": 10},
          {"order": 3, "flows": ["N"], "green": 10, "all_red_after": 10}
        ]
      }
    ],
    "travel_intervals": [{"min_travel": 0, "max_travel": 20}],
    "routes": [
      {"id": "short-green-east", "flow_ids": ["E", "E"], "weight": 10}
    ]
  }'
```

实际响应：

```json
{
  "status": "infeasible",
  "cycle_length": 60,
  "offsets": [0, 0],
  "coordinated_weight": 0,
  "routes": [
    {
      "id": "short-green-east",
      "weight": 10,
      "coordinated": false,
      "departure_windows": [],
      "reason": "green_window_too_short"
    }
  ]
}
```

周期不一致、相位数量/顺序不一致、方案总秒数与周期不一致，或路线引用了对应路口不存在的
车流 ID 时均返回 422，例如：

```bash
curl -i -X POST http://localhost:8000/coordination \
  -H 'Content-Type: application/json' \
  -d '{
    "plans": [
      {
        "cycle_length": 60,
        "phase_count": 1,
        "phases": [{"order": 1, "flows": ["E"], "green": 60, "all_red_after": 0}]
      },
      {
        "cycle_length": 59,
        "phase_count": 1,
        "phases": [{"order": 1, "flows": ["E"], "green": 59, "all_red_after": 0}]
      }
    ],
    "travel_intervals": [{"min_travel": 0, "max_travel": 5}],
    "routes": [{"flow_ids": ["E", "E"], "weight": 1}]
  }'
```

```text
HTTP/1.1 422 Unprocessable Content
```


## 实现说明

- 求解模型见 `app/solver.py`：用 `m[i,r]` 布尔变量表示车流 i 是否位于第 r 个
  环形相位，配合“相位前缀连续使用”约束同时编码**任意分组**与**任意环形顺序**；
  绿灯、各段全红（含末→首）均为整数变量，清空时间下界用跨相位布尔蕴含表达。
- 目标函数 `1000 * cycle + 相位数`（相位数 ≤ 10）实现“周期优先、相位数其次”的
  字典序优化；只有 CP-SAT 返回 `OPTIMAL` 才标记 `optimal`，返回 `FEASIBLE`
  标记 `feasible_candidate`，`INFEASIBLE` 标记 `infeasible`，其余（含超时）
  标记 `unknown`。
- 多路口协调见 `app/coordination.py`：先把每条路线在每个路口的绿灯窗口与累计行驶时间
  区间求差，得到循环整数圆弧；CP-SAT 为每条路线选择统一出发秒和协调布尔变量，
  约束行驶时间闭区间内的每个整数到达时刻均在绿灯窗口中。求解按“最大权重 →
  最小总偏移 → 偏移向量字典序”分阶段固定上一目标后继续优化。
- 输入模型与校验见 `app/models.py`，HTTP 入口见 `app/main.py`。
