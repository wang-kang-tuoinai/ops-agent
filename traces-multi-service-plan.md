# Trace 多服务诊断第一版改动方案

本文记录待实现方案，不代表接口已经完成改造。

## 目标与工具分工

第一版保留从用户请求入口出发的诊断流程，同时允许 search 下钻指定的下游服务：

1. stats：统计已配置对外入口的接口耗时、状态，并汇总下游服务的错误证据。
2. search：按指定服务自己的入口筛选请求，分析该入口及其后代，不受上游或其他分支错误影响。
3. detail：按 trace_id 查看完整已采集链路，保留上游关系供进一步分析。

第一版不增加 scope 参数。stats 和 search 的观察范围不同，需在接口文档、响应和工具描述中明确。stats 的入口请求与 search 的服务入口调用不能混用计数口径。

## 入口概念与示例

假设一条完整 Trace 如下：

```text
gateway：GET /users/:id                         400ms
└─ HTTP client
   └─ user-service：GET /api/v1/users/:id        350ms
      └─ HTTP client
         └─ profile-service：GET /profiles/:id  200ms
```

- 整条链路的根入口是 gateway 的 GET /users/:id。
- user-service 自己的入口是 GET /api/v1/users/:id；该 Span 有上游父节点，仍然是服务入口。
- profile-service 自己的入口是 GET /profiles/:id。
- 第一版服务入口限定为 kind=server 的 HTTP/RPC 服务端 Span，不把 internal/client Span 当作接口入口。
- “某个服务的根 operation”在本方案中统一称为“该服务入口的 operation”，不要求 ParentSpanID 为空。

例如 gateway 的另一条并行分支报错，但 user-service 分支正常：gateway 的 stats 可以是 degraded，而 search(service=user-service) 对应调用仍可以是 ok。

## stats：入口统计与下游错误证据

### 查询范围

- HTTP API 的 service 仍必填，表示对外入口所属服务。
- operation 可选，匹配整条链路根入口的 operation；留空统计该入口服务的全部接口。
- 服务端必须在 Jaeger 候选结果中再次校验根入口的 service、operation 和开始时间，不能把“包含该服务的 Trace”直接当作“以该服务为入口的 Trace”。
- 仅统计可识别的 server 根入口。不因为某个 Span 的父节点缺失，就把该 Span 当成全局根入口。
- Agent 工具可使用可配置的默认入口服务，初始为 ops-agent-backend，让 AI 无需提前知道全部服务名。默认服务必须在工具描述或响应中明确。
- 第一版一次查询一个入口服务。多个独立对外入口可以分别查询，不通过省略 service 隐式扫描所有服务。
- 这份统计代表指定入口的已召回请求样本，不代表全部系统活动；后台任务、异步消费者以及其他入口的请求不在其覆盖承诺内。

### 响应模型

为 EntrypointStat 增加 service 和 downstream_error_services，其余接口耗时与状态字段保留：

```go
type DownstreamErrorService struct {
    Service      string `json:"service"`
    RequestCount int    `json:"request_count"`
}

type EntrypointStat struct {
    Service    string  `json:"service"`
    Operation  string  `json:"operation"`
    Count      int     `json:"count"`
    P50Ms      float64 `json:"p50_ms"`
    P95Ms      float64 `json:"p95_ms"`
    P99Ms      float64 `json:"p99_ms"`
    Failed     int     `json:"failed"`
    Degraded   int     `json:"degraded"`

    DownstreamErrorServices []DownstreamErrorService `json:"downstream_error_services"`
}
```

按 (service, operation) 聚合，避免不同服务同名接口混淆。下游摘要按 request_count 降序、service 升序排列，无匹配项返回空数组。

### 下游错误计数口径

对每个已纳入统计的入口请求：

1. 遍历该入口后代中的全部已采集 Span，不能只使用 findErrorOrigin 返回的代表性节点。
2. 沿用 isErrorSpan：Span 状态为 error，或者记录了异常信息。
3. 根据错误 Span 自己的 service 归属汇总，排除与入口 service 相同的节点。
4. 同一个入口请求内，同一服务无论出现多少个错误 Span，该服务的 request_count 只增加 1。
5. 一个入口请求可以同时计入多个下游服务；这些数量不能相加当作失败请求总数。
6. service 缺失时不根据 operation、IP 或异常文本猜测服务名；给出数据不完整提示。

request_count 表示“包含该下游服务错误 Span 的入口请求数”，不是错误 Span 数、该下游服务自己的失败调用次数，也不是下游服务的错误率。

该摘要统计原始错误证据，与 failed/degraded 分类分别计算。现有入口 HTTP 4xx 被归为 ok 的项目规则暂时保留，因此 ok 分类中也可能观察到后代错误证据；不能假定下游错误计数一定等于或小于 failed + degraded。工具描述需要说明这个例外。

### 错误服务不等于根因服务

- User 的 HTTP client Span 超时，只能证明 User 记录了调用失败，不能据此把 Profile 填入错误服务列表。
- 只有采集到 Profile 自己的错误 Span，才能把 Profile 作为有错误证据的服务纳入计数。
- User、Profile 都出现错误，可能是错误向上传播，不代表两处独立故障。
- Redis/MySQL 客户端 Span 通常属于调用它们的应用服务，不把依赖类型改写为 service。
- stats 不确认根因。需要通过 search、detail、日志以及必要的人工检查继续判断。
- 只有慢、没有错误标记的下游不会出现在该列表；慢请求仍通过入口耗时和 detail 分析。

## search：指定服务入口及其下游

### 参数语义

| 参数 | 第一版语义 |
| --- | --- |
| service | 必填，目标入口 Span 所属服务；本地按服务名精确匹配 |
| operation | 可选，目标服务的入口操作名；不要求属于整条 Trace 的根 Span |
| start/end | 目标入口 Span 的开始时间范围，沿用现有时间参数单位和校验 |
| status | 目标入口及其后代的 ok/degraded/failed 分类 |
| min_duration_ms | 目标入口 Span 的耗时下限 |
| sort | 按目标入口耗时降序或开始时间倒序 |
| limit | 最终返回的服务入口调用数量 |
| fetch_limit | Jaeger 候选 Trace 数量上限，与入口调用数区分 |

不能仅删除当前 t.Root.Service 的校验就算完成多服务支持。需要从完整候选中找出符合 service、kind=server、operation、时间范围的目标入口，再计算每个入口自己的状态和错误摘要。

耗时直接使用目标入口 Span 的 duration_ms，包含其执行期间等待下游的时间，不累加下游 Span 耗时。入口结束后仍运行的异步工作，不自动算入该入口耗时。

状态沿用项目当前规则，但分析起点改为目标入口：目标入口自身错误为 failed；入口正常而后代出错为 degraded；其余为 ok，并保留已有 HTTP 4xx 特例。上游和兄弟分支的错误不得影响该入口分类。

错误摘要只从目标入口及其后代提取，依然是代表性错误证据，不是已确认根因。实际下游错误位置保留在完整 Span 树中供 detail 查看。

### 返回单位与字段

一行对应一次服务入口调用，用 (trace_id, entry_span_id) 标识。同一条 Trace 调用了目标服务两次，两次均符合条件时返回两行，不擅自只取最慢一次或第一条。

```json
{
  "trace_id": "实际 trace_id",
  "entry_span_id": "实际服务入口 span_id",
  "service": "user-service",
  "operation": "GET /api/v1/users/:id",
  "start_ms": 0,
  "duration_ms": 350,
  "status": "degraded",
  "error_summary": {
    "service": "profile-service",
    "span_id": "实际错误节点 span_id",
    "operation": "GET /profiles/:id",
    "message": "示例异常摘要"
  },
  "warnings": []
}
```

- 保留当前摘要字段，增加 entry_span_id；error_summary 增加 service、span_id，均从选中的真实错误节点获取。
- 无错误时不返回 error_summary，不构造虚假的错误节点。
- fetched_count 保持候选 Trace 数口径；matched_count、returned_count 改为入口调用数；has_more_matches 仅表示本批候选中仍有未返回的匹配调用。
- 相同耗时/开始时间时按 trace_id、entry_span_id 排序，保证结果稳定。
- Jaeger 的 service、operation、minDuration 等参数用于候选召回，最终仍按目标入口本地验证，不能把其他 Span 满足条件当作目标入口满足条件。
- 保留候选截断提示，不能宣称空结果证明整个窗口无异常，也不能保证取到全窗口最慢调用。

### 与 stats、detail 的衔接

- 从 gateway 的 stats 发现异常后，可以 search(service=gateway, operation=该入口) 找同一批入口异常请求。
- 若 stats 显示 profile-service 有错误证据，可以 search(service=profile-service) 找该服务的入口异常；不能沿用 gateway 的 operation 作为 Profile 的 operation。
- search 的下游服务查询覆盖该服务在窗口中的候选调用，可能包含其他上游请求。若要核对原入口的那次错误，必须继续使用原请求的 trace_id 查看 detail。
- 不要求 downstream_error_services 中每个服务都能搜到 server 入口；服务可能缺少入口埋点，或只有客户端/内部节点被采集。空结果不否定 stats 中已存在的错误证据。
- detail 继续返回整条已采集链路。它的顶层状态与耗时是整条 Trace 的口径，可能不同于 search 的服务入口摘要；根据 entry_span_id 在树中找到目标入口核对。
- 第一版不强制增加 focus_span_id，也不为 search 返回整棵子树。

## 代码组织与数据完整性

### 数据源、建树与分析分离

- jaeger.go：负责查询 Jaeger、转换 Span 和保留数据完整性提示，不在通用解析阶段强制要求全局根节点必须为 server。
- tree.go：负责父子关系和节点排序。保留父节点缺失的片段及其后代，不能直接丢弃潜在的下游入口。
- analyze.go：提供以任意目标入口为起点的状态分析、代表性错误提取、下游错误服务去重等函数。
- stats.go：选择符合根入口契约的请求，按入口分组，并汇总下游错误计数。
- search.go：选择符合服务入口契约的 Span，按目标入口筛选和排序。

内部可增加 EntryView 表达 trace_id 与入口 Span 的关联，引用原有 Span，避免为每个入口复制整条 Trace。不能把 Trace.Root 临时替换成下游入口或清空其 ParentSpanID，否则会破坏 detail 的完整链路语义。

### 不完整链路

- 保留全部可用 Span 及缺失父节点的标记；服务入口选择不能只遍历当前唯一 Root 可达的节点。
- 上游缺失但目标服务入口存在时，search 可以基于其已采集子树返回结果，并给出链路不完整提示。
- stats 不把“父节点缺失”的服务入口升级为已确认的全局根入口；多个根节点无法明确识别时也不能任意选择后当作完整链路统计。
- 缺失 Span 时，ok 只表示当前已采集范围内未发现符合规则的错误，不能证明没有遗漏异常。
- 保留 detail 的裁剪和缺失提示，不把单个可见片段描述为完整链路。

## 涉及文件

- obs-api/internal/tracestore/stats.go、search.go：聚合和筛选语义。
- obs-api/internal/tracestore/analyze.go、tree.go、provider.go：入口视图、通用分析与缺失片段保留。
- obs-api/internal/tracestore/jaeger.go：解析与入口过滤解耦、候选提示。
- obs-api/internal/handler/trace_stats.go、trace_search.go、trace_response.go：参数、响应和计数口径。
- ops-diagnosis-agent/langgraph_tools.py：入口服务参数、服务归属、上下游分析范围及证据限制。
- obs-api/docs 中的 Trace 接口说明，以及项目相关观测口径文档。

## 实施顺序

1. 建立多服务合成 Trace 测试数据，明确根入口、服务入口、重复调用和缺失上游的预期结果。
2. 解耦通用建树与入口过滤，增加以目标入口为起点的分析能力。
3. 修改 stats 的根入口校验、响应模型和下游错误计数。
4. 修改 search 的服务入口筛选、摘要和计数单位。
5. 同步 Agent 工具描述、接口文档和相关知识库口径。
6. 在实际传播 Trace Context 的跨服务调用上验证 stats → search → detail 的结果能对应。工具本身不能把未传播上下文的独立 Trace 自动拼接。

## 验收用例

| 场景 | 预期 |
| --- | --- |
| 单服务正常请求 | 原有入口耗时和状态不变，下游错误服务为空 |
| 多个服务存在相同 operation | 按 service 区分，不混合统计或返回 |
| 一个下游服务在同一入口请求内出现多个错误 Span | 该服务 request_count 只加 1 |
| 同一入口请求内两个下游服务均有错误 | 分别计数，不把计数总和解释为失败请求数 |
| User 客户端超时，没有 Profile 的错误 Span | 不推断 Profile 已记录错误 |
| 只有上游或兄弟分支报错 | search 查询正常分支仍为 ok，错误摘要不泄漏其他分支 |
| 下游错误，上游成功兜底 | 根入口可以 degraded；下游自身入口可以 failed |
| 同一 Trace 两次调用目标服务 | search 返回两个 entry_span_id 不同的调用 |
| 下游入口慢，上游总耗时更长 | search 的排序、阈值均使用目标入口耗时 |
| 入口为 HTTP 4xx，同时有后代错误 | 保留既有状态分类，错误证据摘要按独立口径计数 |
| 全局根不是 server，但下游有 server 入口 | 不因根类型直接丢弃目标服务的 search 结果 |
| 目标入口有缺失的上游父节点 | search 可返回并告警，stats 不误认其为全局根 |
| Jaeger 候选达到上限或 detail 裁剪 | 保留采样、缺失与裁剪限制提示 |

## 第一版不包含

- 自动枚举全部服务并生成全系统健康结论。
- 消息消费者等异步入口的统计，以及任意 internal/client Span 搜索。
- 自动确定根因、跨服务耗时直接求和、复杂关键路径归因。
- Publisher/Consumer 业务改造或新增跨服务链路埋点；如需真实多服务演示，应作为配套任务明确处理。
