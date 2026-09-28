# Trace stats 按服务入口统计：接口设计

状态：已于 2026-09-28 实现。实际接口说明见 [traces-stats.md](obs-api/docs/traces-stats.md)。默认仍只查询一个服务。

后续查询预算调整已实现：未指定 operation 时先发现 server 操作，各查询最多 1500 条候选；指定 operation 时最多 5000 条。移除对外 limit，以 operation_queries 报告各接口查询状态。以下已同步该契约。

stats 已从只统计指定服务的全局 server 根入口，改为与 search 一致，统计指定服务的 server 入口及其后代，不要求入口位于整条 Trace 的根部。本文取代第一版方案中 stats 的根入口限制，其他已确定的错误判定和 detail 行为继续保留。以下记录本次实现采用的接口设计。

## 1. 目标与统计范围

一次查询一个 service，按该服务入口的 operation 汇总耗时、状态和下游错误服务。不增加 scope 参数，也不通过省略 service 查询所有服务。

```text
gateway：GET /users/:id
├─ user-service：GET /api/v1/users/:id
│  └─ profile-service：GET /profiles/:id
└─ order-service：GET /orders
```

查询 `service=user-service` 时，统计 user-service 的入口及其后代 profile-service。gateway 和 order-service 的耗时、错误不参与这次入口的计算。

- 入口限定为 `kind=server`，不把 client/internal Span 当作接口入口；异步消费者暂不纳入。
- 入口可以有上游父节点，也可以是全局根入口。
- operation 指该服务入口自己的操作名，不是上游接口名或任意后代操作名。
- 一个 Trace 内存在多个匹配入口时，每个入口分别计为一次调用，包括同一服务被重复调用或再次进入的情况。
- 若两个匹配入口存在祖先关系，它们仍分别统计；其观察子树可能重叠，调用量不能当作独立用户请求量。

## 2. 请求接口

`GET /api/v1/traces/stats`

| 参数 | 必填 | 设计口径 |
| --- | --- | --- |
| service | 是 | 精确匹配服务入口的 service；空白值返回 400 |
| operation | 否 | 精确匹配服务入口的 operation；不传则统计该服务所有匹配接口 |
| start / end | 否 | 秒级 Unix 时间戳；沿用现有时间解析，默认最近一小时，窗口最长七天 |

不再接受 limit，传入返回 400。预算由服务端配置：TRACE_STATS_PER_OPERATION_LIMIT 默认 1500，TRACE_STATS_FOCUSED_LIMIT 默认 5000，须满足 `1 <= 前者 < 后者 <= 5000`。

本地按入口开始时间筛选，边界沿用 search：`start_ms <= entry.start_ms <= end_ms`。不是按根 Span 开始时间，也不是按时间区间是否相交筛选。

本次不增加 status、min_duration_ms、sort 参数，stats 汇总全部匹配入口的分布；按状态或慢请求下钻继续使用 search。

请求示例：

```http
GET /api/v1/traces/stats?service=user-service&operation=GET%20%2Fapi%2Fv1%2Fusers%2F%3Aid&start=1789479485&end=1789479545
```

HTTP API 仍要求 service。Agent 工具可省略该参数，使用现有 `TRACE_ENTRY_SERVICE` 配置，默认 `ops-agent-backend`。暂不重命名环境变量，但工具描述须明确它是默认查询服务，不要求是全局入口服务。

## 3. 入口筛选与聚合流程

```text
校验请求参数
  → 未指定 operation 时查询 server 操作目录，去重后按名称排序；指定时跳过目录
  → 按 service / 每个 operation / 时间窗口查询，各 1500 条；聚焦单接口时 5000 条
  → 解析已采集 Span 森林，保留缺失上游的片段
  → 共用入口选择逻辑：kind=server + service + operation + 入口开始时间
  → 按 (trace_id, entry_span_id) 去重
  → 针对每个入口及其后代计算状态和下游错误服务
  → 按 (service, operation) 聚合耗时、计数
  → 返回统计及数据范围提示
```

Jaeger 命中任意 Span 不等于该服务入口满足条件，必须进行本地校验。stats 应使用全部匹配入口，不能直接调用已经排序、limit 截断的 search 响应来聚合。

候选预算限制的是每个 operation 的 Trace 数，不是入口调用数。每批只筛选当前 operation 的入口，同一 Trace 中其他操作不混入该批；最终按 (trace_id, entry_span_id) 去重。`fetched_traces` 跨查询按 trace_id 去重，不能对各批原始数量简单求和。

目录不支持时间范围筛选，可能包含当前窗口没有数据的历史操作。最多 3 路并发，查询共用 25 秒预算，单次最多执行 30 个 operation。超出数量预算或超时前未启动的操作标为 skipped；查询失败标 failed，不能作为零请求处理。概览触顶时提示指定接口以更高预算重查，单接口触顶时提示缩小窗口。本版不递归拆分时间。

本地筛选只能检查已经召回的数据，不能弥补 Jaeger 查询语义、采样、候选上限或采集缺失造成的遗漏。结果始终是已召回样本的统计。

## 4. 耗时和状态

### 耗时

使用入口 Span 自身的 `DurationMs` 计算 P50/P95/P99，包含入口执行期间等待下游的时间，不将后代耗时累加，也不使用整条 Trace 的耗时。

百分位计算方式和小样本提示沿用现有实现。分组按 count 降序排列，同数时按 service、operation 升序，保持稳定顺序。

### 状态

| 状态 | 判定 |
| --- | --- |
| failed | 当前入口自身存在有效错误 |
| degraded | 当前入口没有有效错误，但后代存在有效错误 |
| ok | 当前入口及已采集后代均没有有效错误 |

三类状态互斥。`ok` 不等于 HTTP 2xx 或业务成功；`degraded` 不保证实际执行了业务降级，也不保证请求变慢。只有耗时高而没有错误证据时，仍可能为 ok。

继续复用 stats、search、detail 分析所用的有效错误规则：Span 标记 error、记录异常信息或存在 HTTP 5xx 等错误证据时参与判定，并沿用已确认的 MySQL 重复键业务冲突排除规则。

重复键排除仍要求同一服务 `mysql.Create` / `mysql.Update` 的 `user.duplicate=true` 业务标记，以及对应 MySQL Span 的数据库类型和明确的重复键消息符合条件。不能仅凭 1062 忽略所有错误，不能忽略整棵子树，也不恢复“所有 4xx 直接判 ok”的旧规则。

detail 保留原始错误及 `expected_error` 标记。已排除重复键与 Redis 超时同时出现时，Redis 错误仍参与分类和下游计数。

## 5. DownstreamErrorServices

保留每个接口分组里的 `downstream_error_services`，结构不变：

```go
type DownstreamErrorService struct {
    Service      string `json:"service"`
    RequestCount int    `json:"request_count"`
}
```

对每一次选中的服务入口调用：

1. 遍历该入口全部已采集后代，不能只使用 findErrorOrigin 的代表性节点。
2. 使用与状态分类相同的有效错误规则，包括已处理重复键的排除。
3. 按错误 Span 自己的 service 归属汇总，排除与当前入口同名的服务。
4. 同一次入口调用内，同一服务无论出现多少个错误 Span，request_count 只增加 1。
5. 同一次调用可以计入多个下游服务；这些计数不能相加当作失败调用总数。
6. service 缺失时不猜测名称，不纳入具名服务计数，并在 notices 提示。该错误仍参与入口状态判定。

`request_count` 的含义调整为“包含该下游服务有效错误 Span 的当前服务入口调用次数”，不是整条 Trace 去重后的数量，也不是该下游服务自己的失败次数或错误率。

按 request_count 降序、service 升序排列；无匹配服务时返回 `[]`。每个下游服务的计数不会超过本组的 count；在相同有效错误口径下，也不会超过本组 failed + degraded，但多个服务计数之和可能超过它们。

错误服务不等于根因服务。例如 user-service 的 HTTP client Span 超时，只能证明 user-service 记录了错误，不能据此把 profile-service 填入下游列表。Redis/MySQL 客户端 Span 通常属于调用方服务，组件名不能直接当作 service 名称。

## 6. 响应模型

保留顶层 `service`、`stats`、`notices`。将 `stats.total_traces` 改为 `stats.total_calls`，明确统计单位；增加 `meta` 说明查询窗口和候选数量。

```json
{
  "service": "user-service",
  "stats": {
    "total_calls": 3,
    "by_status": {"ok": 1, "degraded": 1, "failed": 1},
    "entrypoints": [
      {
        "service": "user-service",
        "operation": "GET /api/v1/users/:id",
        "count": 3,
        "p50_ms": 100,
        "p95_ms": 300,
        "p99_ms": 300,
        "failed": 1,
        "degraded": 1,
        "downstream_error_services": [
          {"service": "profile-service", "request_count": 2}
        ]
      }
    ]
  },
  "meta": {
    "window": {"start": 1789479485, "end": 1789479545},
    "per_operation_limit": 1500,
    "fetched_traces": 2,
    "operation_queries": [{
      "operation": "GET /api/v1/users/:id",
      "status": "success",
      "raw_trace_count": 2,
      "limit_reached": false
    }]
  },
  "notices": [
    "仅统计已召回的指定服务 server 入口调用及其后代；下游错误服务不代表根因。",
    "样本较少，耗时百分位仅供参考。"
  ]
}
```

上例字段和数值用于说明响应结构；百分位实际取值以现有算法为准。

字段约定：

- total_calls：去重后匹配入口的数量。
- by_status：相同入口样本的状态分布，三个键固定返回，数量之和等于 total_calls。
- entrypoints[].count：该接口的入口调用数；所有分组 count 之和等于 total_calls。
- 每组 ok 数量可用 `count - failed - degraded` 得到，暂不新增字段。
- meta.window：服务端解析后实际使用的窗口，单位为秒。
- meta.per_operation_limit：本次每个 operation 的实际候选上限，取代 fetch_limit。
- meta.operation_queries：各操作的 operation、status（success/failed/skipped）、raw_trace_count、limit_reached 和可选 message。成功无数据时 raw_trace_count 为 0，失败/跳过为 null。原始数量包含解析失败的 Trace；达到上限表示可能截断，不是已确认截断。
- meta.fetched_traces：provider 成功解析并交给本地筛选的不同 trace_id 数，不能解释为窗口内全部 Trace 数。
- notices：候选截断风险、小样本、数据缺失等提示；同一 Trace 多个入口不重复堆积同一条提示。

成功查询但无匹配入口时返回 200，total_calls 和三类状态计数为 0，entrypoints 为 `[]`，不伪造零毫秒接口分组。部分操作失败时返回 200 和显式不完整提示；全部失败/未执行时返回 502，并保留 operation_queries。发现目录失败返回 502 error。未知服务空目录与故障不同，不宣称系统正常。

字段改名是响应契约变更。实现时同步修改调用方、响应模型、工具描述、测试和接口文档，不同时用 total_traces 表达调用数，避免两个名称产生歧义。

## 7. 缺失数据与下钻

- 缺少全局根入口，但有可识别的目标服务 server Span 时，仍纳入统计，并保留数据不完整提示。
- 若错误片段无法通过父子关系连接到目标入口，不能仅因属于同一 Trace 就计入该入口子树。
- 缺少后代可能低估错误和下游影响；没有发现错误不代表采集完整。
- stats 中的 service / operation 可以原样传给 search。对单个 operation 的同一批候选和相同基础筛选条件，该分组 count 应与 search 在状态、耗时过滤及结果截断之前的匹配入口数一致；实际 stats 分批查询且预算更大，候选集合可能不同。
- 两次独立 HTTP 查询的数据可能变化，不承诺数值始终一致。
- search 返回 trace_id + entry_span_id；detail 仍展示整条已采集 Trace，使用 entry_span_id 定位本次入口，detail 的全局状态不一定等于该入口状态。
- 根据下游服务名发起新查询可能包含来自其他上游的调用，不能把新的查询结果都当作原入口的下游影响。

## 8. 实现拆分

1. 在 tracestore 提取 stats/search 共用的服务入口选择函数，返回原 Trace 与选中 Span 的关联，不修改 Trace.Root，不破坏原始树。
2. 共用函数负责 server、service、operation、时间条件以及 (trace_id, span_id) 去重；遍历所有已解析森林。
3. stats 聚合选中入口；search 在共用选择之后继续进行状态、耗时筛选、排序和 limit 截断。
4. 保留 classifyStatus、有效错误判定、下游服务去重和耗时百分位能力，调整其输入范围，不另写一套错误规则。
5. handler 负责参数和响应组装；tracestore.CollectStats 负责发现操作、并发查询、预算、每批筛选及统一聚合；provider 返回 TraceBatch（原始数量、解析结果、提示），不根据提示文字反推是否截断。
6. 更新 Agent 工具描述、obs-api/docs/traces-stats.md 和相关维护文档，清除“只能统计全局根入口”的旧说明。

## 9. 验证场景

| 场景 | 预期 |
| --- | --- |
| 单体 ops-agent-backend 正常请求 | 原有耗时、状态保持一致，字段迁移后可正常调用 |
| gateway → user-service → profile-service | 查询 user-service 可统计非全局根入口，耗时取 user-service 入口 |
| 上游或兄弟分支错误 | 不影响正常的 user-service 入口状态和下游计数 |
| 同一 Trace 两次进入 user-service | total_calls 计 2；重复候选不重复计数 |
| 同服务嵌套 server 入口 | 每个匹配入口独立计算，明确子树可能重叠 |
| 下游同服务多个错误 Span | 当前入口内该服务 request_count 只加 1 |
| 下游两个不同服务报错 | 两个服务各加 1，入口状态只计一次 |
| 仅有已处理 MySQL 重复键 | 不计有效错误，detail 原始错误仍保留 |
| 已处理重复键加 Redis 超时 | Redis 错误仍参与入口状态判定 |
| 缺失上游、存在目标入口 | 仍可统计，返回不完整提示 |
| 服务名缺失或错误片段无法连接 | 不猜测下游归属，提示证据不完整 |
| operation、窗口边界、非 server Span | 筛选口径与 search 一致 |
| 候选数达到上限、没有匹配入口 | 正确提示范围限制或返回空统计 |
| 未指定/指定 operation | 分别使用 1500/5000；聚焦查询不依赖目录发现 |
| 部分失败、全部失败、超时、操作过多 | 分别报告 failed/skipped/null 数量，不误报零调用或完整统计 |

先用合成多服务 Trace 和 handler 测试验证范围与计数，再用当前单体的正常请求、重复键、Redis 超时场景做回归。真实跨服务传播和采集完整性留待多服务联调验证。
