# 路口信号周期编排与多路口绿波协调服务

本服务包含两个基于 [OR-Tools CP-SAT](https://developers.google.com/optimization/cp/cp_solver) 的接口：

- **`POST /plans`（单路口周期编排）**：给定 2～10 条有向车流、各自可接受的绿灯秒数区间、
  无向冲突对、有向清空时间与周期上限，联合决定：
  1. **分组**：每条车流恰好放入一个相位放行；同相位不得包含相互冲突的车流；
  2. **顺序**：相位在周期中的环形排列（末相位之后回到首相位）；
  3. **整数秒时长**：相位绿灯必须同时落在所有成员车流的绿灯区间内；
     相邻相位（含末相位 → 首相位）之间插入**全红**，时长不少于所有跨相位
     有向车流对（离开相位车流 → 进入相位车流）清空时间的最大值；
     整个周期只有一个相位时全红为 0。

  优化目标为字典序：**先最小化周期总秒数，周期并列时再最小化相位数**。

- **`POST /coordination`（多路口绿波协调）**：接收 2～8 个**按行驶方向排列且周期相同**的
  已生成路口方案、相邻路口之间的整数行驶时间**闭区间**，以及由各路口车流 ID 组成、
  带正整数权重的目标路线，计算每个路口相对首个路口的整数秒偏移
  （`0 ~ 周期-1`，首个路口固定为 `0`），使可连续通过路线的权重总和最大。

技术栈：Python 3.12 · FastAPI · OR-Tools（CP-SAT）· Docker Compose。

## 一键启动

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`。

- 健康检查：`GET http://localhost:8000/health`
- 单路口编排接口：`POST http://localhost:8000/plans`
- 多路口绿波协调接口：`POST http://localhost:8000/coordination`
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

### 请求格式

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `intersections` | 数组，2～8 项 | 按行驶方向排列的已生成路口方案，结构与 `/plans` 响应中的 `plan` 一致，另加非空 `id` |
| `intersections[].cycle_length` | 整数 | 所有路口周期**必须相同**，否则 422 |
| `intersections[].phases` | 数组 | 相位 `order` 必须恰为 `1..N`；同一车流不得出现在多个相位；各相位 `green + all_red_after` 之和必须等于 `cycle_length`；`all_red_after ≤ 10`；单相位时全红必须为 0 |
| `intersections[].phase_count` | 整数 | 必须等于实际相位数 |
| `travel_times` | 数组，恰为 K-1 项 | 相邻路口（第 k→第 k+1 个）的整数行驶时间闭区间 `[min_seconds, max_seconds]`（秒，含端点），`0 ≤ min ≤ max ≤ 3600` |
| `routes` | 数组，1～32 项 | 目标路线；`id` 非空且唯一、`weight` 为正整数（≤ 10⁹） |
| `routes[].flows` | 字符串数组，恰为 K 项 | 按路口顺序给出该路线在每个路口借用的车流 ID；车流必须在**对应位置**路口的方案中存在，否则 422 |

相位结构、周期或车流引用不一致（含车流不在对应路口、区间反向、数量不符等）
均返回 **HTTP 422**。

### 协调语义

设公共周期为 `C`，路口 k 相对首个路口的偏移为 `o[k]`（`o[0]=0`，`0 ≤ o[k] < C`），
第 j 段行驶时间可在闭区间 `[L_j, U_j]` 内取任意整数秒。一条路线可**连续通过**
（`coordinated=true`）当且仅当存在一个**统一出发秒** `d ∈ [0, C-1]`，使得到达
每个路口的所有可能整数时刻

```
d + Σ_{j<k} L_j  ~  d + Σ_{j<k} U_j        （再对 C 取模）
```

都落在该路口对应车流随偏移平移后的绿灯窗口内（即整段到达区间必须被绿灯完整覆盖，
允许跨过周期边界循环）。

优化目标（字典序）：

1. **可连续通过路线的权重总和最大**；
2. 并列时**总偏移量** `Σ o[k]` 最小；
3. 再并列时**偏移向量** `(o[1], o[2], …)` 字典序最小。

### 响应格式

| 字段 | 含义 |
| --- | --- |
| `status` | `optimal` / `feasible_candidate` / `infeasible` / `unknown` |
| `cycle_length` | 公共周期 |
| `offsets` | 各路口相对首个路口的偏移秒数，首个恒为 0；`infeasible`/`unknown` 时为 `null` |
| `coordinated_weight` | 返回偏移下可连续通过路线的权重总和 |
| `routes[].coordinated` | 该路线在返回偏移下是否存在统一出发秒 |
| `routes[].departure_windows` | 可通行出发秒窗口列表，每项为闭区间 `[start, end]`（含端点、不跨周期边界，跨边界会拆成两段）；不可通行时为 `[]` |
| `routes[].reason` | 未协调原因（可通行时为 `null`） |

`status` 语义：

| status | 含义 |
| --- | --- |
| `optimal` | 已证明权重总和最大，并在此前提下取到最小总偏移与字典序最小偏移向量 |
| `feasible_candidate` | 时限内找到可行候选（含权重或偏移择优未完成最优性证明） |
| `infeasible` | 已证明**不存在任何能协调一条目标路线的偏移**（此时 `offsets` 为 `null`） |
| `unknown` | 时限内既未找到可行候选，也未能证明无解（`offsets` 为 `null`） |

> 注意：某条路线因到达时刻的行驶时间极差（区间宽度累计）大于其某路口绿灯长度而
> 在**任何偏移**下都不可能协调时，它会被排除，但只要还有其他路线可协调，
> 整体状态仍可能是 `optimal`，该路线在 `routes[].reason` 中给出具体原因。

### 协调接口请求 / 响应示例

以下示例均为对实际运行服务的真实调用与输出，请求体见 `examples/`。

#### 1. 最优绿波（optimal）：三路口双向协调

三个同周期 60 秒的路口，东西向、南北向各一条目标路线；相邻路口行驶时间恰为一个
周期（60 秒），故零偏移即可让两个方向都拿到完整绿波带：

```bash
curl -s -X POST http://localhost:8000/coordination \
  -H 'Content-Type: application/json' \
  --data @examples/coordination_green_wave.json
```

实际响应（格式化后）：

```json
{
  "status": "optimal",
  "cycle_length": 60,
  "offsets": [0, 0, 0],
  "coordinated_weight": 15,
  "routes": [
    {
      "id": "east-west-arterial",
      "weight": 10,
      "coordinated": true,
      "departure_windows": [[0, 19]],
      "reason": null
    },
    {
      "id": "north-south-cross",
      "weight": 5,
      "coordinated": true,
      "departure_windows": [[30, 49]],
      "reason": null
    }
  ]
}
```

校验：东西向车流绿灯位于本地秒 `0~19`，南北向位于 `30~49`；行驶 60 秒（一个周期）
后到达时刻模 60 不变，`offsets=[0,0,0]` 时东西向在出发秒 `0~19`、南北向在 `30~49`
均可连续绿灯通过三个路口，两条路线权重和 `10+5=15` 全部协调。

#### 2. 权重竞争（optimal）：保高权重、放弃低权重路线

两个 50 秒周期的三相位路口，两条路线要求互斥的偏移；求解器保证权重总和最大，
保留权重 10 的主干路线，放弃权重 5 的次要路线，并给出其未协调原因：

```bash
curl -s -X POST http://localhost:8000/coordination \
  -H 'Content-Type: application/json' \
  --data @examples/coordination_competing.json
```

实际响应（格式化后）：

```json
{
  "status": "optimal",
  "cycle_length": 50,
  "offsets": [0, 6],
  "coordinated_weight": 10,
  "routes": [
    {
      "id": "major-arterial",
      "weight": 10,
      "coordinated": true,
      "departure_windows": [[0, 0]],
      "reason": null
    },
    {
      "id": "minor-cross",
      "weight": 5,
      "coordinated": false,
      "departure_windows": [],
      "reason": "在当前最优偏移下出发秒无法同时满足全部路口：最先无法对齐的是路口2('B2')的车流 'WB'绿灯窗口（偏移已优先让权重总和最大的路线连续通过，该路线未被纳入）"
    }
  ]
}
```

#### 3. 证明无解（infeasible）

唯一一条路线在第二路口借用的车流只有 10 秒绿灯，而该段行驶时间区间 `[20,40]`
宽达 20 秒：区间内的到达时刻不可能全部落进 10 秒绿灯，**任何偏移都无法协调**：

```bash
curl -s -X POST http://localhost:8000/coordination \
  -H 'Content-Type: application/json' \
  --data @examples/coordination_infeasible.json
```

实际响应（格式化后）：

```json
{
  "status": "infeasible",
  "cycle_length": 60,
  "offsets": null,
  "coordinated_weight": 0,
  "routes": [
    {
      "id": "ew-route",
      "weight": 10,
      "coordinated": false,
      "departure_windows": [],
      "reason": "到达路口2('C2')车流 'EW' 的行驶时间极差累计已达 20 秒，超过该路口绿灯长度 10 秒，区间内的到达时刻不可能全部落在绿灯窗口，任何偏移都无法协调该路线"
    }
  ]
}
```

#### 4. 非法输入（HTTP 422）

例如第二个路口周期为 50，与首个路口的 60 不一致：

```bash
curl -i -X POST http://localhost:8000/coordination \
  -H 'Content-Type: application/json' \
  -d '{
    "intersections": [
      {"id": "A1", "phases": [
        {"order": 1, "flows": ["EW"], "green": 20, "all_red_after": 10},
        {"order": 2, "flows": ["NS"], "green": 20, "all_red_after": 10}
      ], "cycle_length": 60, "phase_count": 2},
      {"id": "A2", "phases": [
        {"order": 1, "flows": ["EW"], "green": 20, "all_red_after": 10},
        {"order": 2, "flows": ["NS"], "green": 20, "all_red_after": 10}
      ], "cycle_length": 50, "phase_count": 2}
    ],
    "travel_times": [{"min_seconds": 25, "max_seconds": 25}],
    "routes": [{"id": "R1", "weight": 10, "flows": ["EW", "EW"]}]
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
      "msg": "Value error, 路口 'A2' 的周期 50 与首个路口的周期 60 不一致，协调只支持相同周期",
      "input": { "...": "（回显请求体）" },
      "ctx": { "error": {} }
    }
  ]
}
```

相位 `order` 不连续、绿灯与全红之和不等于周期、同一车流出现在多个相位、
路线车流在对应路口不存在、`travel_times` 数量不为 K-1、行驶区间反向、
权重非正、路口/路线 id 重复、路口数不在 2～8、出现未知字段等同样返回 422。

## 实现说明

### `POST /plans`

- 求解模型见 `app/solver.py`：用 `m[i,r]` 布尔变量表示车流 i 是否位于第 r 个
  环形相位，配合“相位前缀连续使用”约束同时编码**任意分组**与**任意环形顺序**；
  绿灯、各段全红（含末→首）均为整数变量，清空时间下界用跨相位布尔蕴含表达。
- 目标函数 `1000 * cycle + 相位数`（相位数 ≤ 10）实现“周期优先、相位数其次”的
  字典序优化；只有 CP-SAT 返回 `OPTIMAL` 才标记 `optimal`，返回 `FEASIBLE`
  标记 `feasible_candidate`，`INFEASIBLE` 标记 `infeasible`，其余（含超时）
  标记 `unknown`。

### `POST /coordination`

- 模型与校验见 `app/coordination.py`，HTTP 入口见 `app/main.py`。
- 先按相位顺序把每条被引用车流的绿灯换算成本地周期窗口 `[s_k, e_k]`；
  对每条路线引入“是否协调”布尔变量与统一出发秒 `d`，偏移 `o[k] ∈ [0,C-1]`。
  到达路口 k 的整数时刻覆盖区间
  `[d+ΣL, d+ΣU]`，用辅助整数 `m`（周期倍数）要求整段区间落入循环绿灯窗口
  `[s_k+o[k]+mC, e_k+o[k]+mC]`，从而在不取模布尔变量的前提下精确处理跨边界。
- 优化用分阶段 CP-SAT 完成严格字典序：阶段 0 最大化协调权重和；之后每证明一层
  最优（总偏移、再依次 `o[1],o[2],…`）就把该最优值作为等式锁进后续模型，
  防止后一层破坏前一层。仅当全部阶段均返回 `OPTIMAL` 才标记 `optimal`。
- 响应中的出发窗口由返回偏移直接枚举 `d ∈ [0,C-1]` 精确计算并合并为闭区间，
  与求解模型语义一致；结构性无解（极差大于绿灯长度）在建模前预检并给出原因。
