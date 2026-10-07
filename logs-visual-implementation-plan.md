# 日志可视化与 Trace 双图联动实施方案

状态：第一版代码已实现，本文件保留设计决策。实际接口与部署步骤见 [日志可视化接口](obs-api/docs/logs-visual.md)。真实数据库索引是否已建立以部署检查为准。

## 1. 第一版目标

在现有 Trace 面板下增加日志级别堆叠柱状图，两张图共享服务、显示时间窗口、HTTP 接口筛选与框选范围。用户在任一图中选择时间，两张图同步高亮，并更新同一份诊断草稿，不自动发送。

日志数据由 obs-api 在数据库中按时间桶聚合。前端读取内存缓存快照，后台刷新缓存，与现有 Trace 面板采用相同的交互模式。第一版不增加聚合表，不使用 Redis 保存面板缓存，不扩展 Agent 工具。

## 2. 已核对的代码与筛选口径

- `ops-agent-backend/internal/observability/middleware.go` 在执行 Handler 前把 route、method 写入请求 Context。
- `recorder.go` 从 Context 自动补充这两个字段，因此沿用请求 Context 的业务、缓存等结构化日志也具有接口归属。
- 未匹配 HTTP 路由时，当前代码记录 `route=<unmatched>`，不是空字符串。
- `obs-api/internal/logstore/mysql.go` 的 QueryStats 当前按 service、level 聚合整个窗口，没有时间桶。
- 现有日志索引包括 `(service, level, ts)`；新增 `(service, ts)` 用于查询指定服务、时间段内的全部日志级别。

| 面板条件 | Trace 条件 | 日志条件 |
| --- | --- | --- |
| 某服务，全部接口 | service | service，不过滤 method/route |
| 某服务，`GET /api/v1/users/:id` | service + operation | service + method=GET + route=/api/v1/users/:id |

前端识别当前项目的标准 `HTTP方法 + 空格 + 路由模板` operation，得到 method、route。使用路由模板，不使用 `/users/3344` 这样的实际用户路径。

选择具体 HTTP 接口时，未匹配路由、其他接口以及空路由日志被排除，这是预期行为。全部接口模式保留这些日志。若未来出现无法映射的非 HTTP operation，明确显示“该 operation 暂不支持日志联动”，不静默展示全部日志冒充已筛选结果。

日志 ERROR 条数不等于失败请求数；同一个请求可以产生多条日志。Trace 的状态分类不参与日志级别筛选。

## 3. 后端接口

### 3.1 新增日志图表接口

```http
GET /api/v1/visual/logs
```

| 参数 | 第一版规则 |
| --- | --- |
| service | 必填，去除首尾空格后非空，最多 64 字节，与日志模型一致 |
| method | 可选，与 route 同时提供；标准大写 HTTP 方法 |
| route | 可选，与 method 同时提供；路由模板，最多 255 字节 |
| start_ms / end_ms | 同时省略为实时最近 15 分钟；同时提供为固定历史窗口 |

历史窗口须满足 start_ms > 0、end_ms > start_ms、长度不超过 15 分钟，未来时间容忍与 Trace 接口一致（5 秒）。非法参数返回 400，不静默重置。

第一版桶宽固定 10 秒，响应返回 `bucket_ms=10000`，不开放任意桶宽参数。15 分钟约 90 个桶；未对齐边界时最多 91 个桶。所有级别一起聚合，前端切换图例不重新查数据库。

### 3.2 响应模型

在 logstore 中定义专用查询、结果、缓存响应模型；以下示意字段结构，数组仅展示一个桶：

```json
{
  "service": "ops-agent-backend",
  "method": "GET",
  "route": "/api/v1/users/:id",
  "window": {"start_ms": 1791000000000, "end_ms": 1791000900000},
  "data_window": {"start_ms": 1791000000000, "end_ms": 1791000900000},
  "bucket_ms": 10000,
  "buckets": [
    {
      "start_ms": 1791000000000,
      "end_ms": 1791000010000,
      "total": 26,
      "by_level": {"DEBUG": 0, "INFO": 20, "WARN": 4, "ERROR": 2}
    }
  ],
  "summary": {
    "total": 26,
    "by_level": {"DEBUG": 0, "INFO": 20, "WARN": 4, "ERROR": 2}
  },
  "loading": false,
  "initialized": true,
  "stale": false,
  "updated_at_ms": 1791000900100,
  "data_as_of_ms": 1791000900000,
  "refresh_seconds": 15,
  "notices": []
}
```

字段约定：

- `window`：本次快照请求的观察窗口。实时模式随当前时间前移，历史模式固定。
- `data_window`：最后一次成功 SQL 聚合实际使用的范围。未成功查询过时为 null。
- `buckets`、`summary`：严格属于 data_window，不能因读取缓存而伪装成已更新到 window 的末端。
- `summary` 由全部桶相加得到，不额外查询 logs/stats。示意 JSON 的 summary 只对应其中示意桶；真实响应必须包含整个 data_window 的桶和对应总数。
- `loading`：后台任务已排队或正在执行。
- `initialized`：至少成功完成过一次查询；成功查到零日志也是 true。
- `stale`：尚未初始化、最近刷新失败，或实时数据截止时间落后超过两个刷新周期。历史快照按查询年龄判断是否需要更新，不因查询的是旧日期而永远 stale。
- `updated_at_ms`：成功写入缓存的时间；`data_as_of_ms`：成功查询的窗口末端，不表示日志已完整入库的水位。
- `error`：可选，最近查询失败时返回适合用户阅读的错误，不泄露 SQL 或连接信息。
- `notices`：包括数据时间范围与显示窗口不同、边界桶不满 10 秒等必要提示。

查询失败时不补零，不覆盖旧结果。首次加载/首次失败为 buckets=[]、summary=null、data_window=null；成功查到零条时返回已补零的桶和零汇总。

合法快照请求返回 200，通过状态字段区分加载与失败；所有缓存条目都在加载而无法分配新条目时返回 429。响应设置 `Cache-Control: no-store`，HTTP 缓存和后端应用内存缓存是两回事。

### 3.3 数据库聚合

新增 `QueryHistogram`，不在 handler 内拼 SQL，不在 Go 内加载原始日志重新计数。查询示意：

```sql
SELECT (ts DIV ?) * ? AS bucket_start_ms, level, COUNT(*)
FROM logs
WHERE service = ? AND ts >= ? AND ts < ?
  -- 选择具体 HTTP 接口时追加：AND method = ? AND route = ?
GROUP BY bucket_start_ms, level
ORDER BY bucket_start_ms, level;
```

参数绑定，桶宽为毫秒。WHERE 保留对原始 ts 的范围过滤，时间分桶表达式只放在 SELECT/GROUP BY。

桶按 Unix 时间的 10 秒整数边界对齐，便于刷新后位置稳定。查询窗口为 `[start_ms, end_ms)`；首尾桶只统计窗口内的日志，返回的 start_ms/end_ms 裁切到实际查询边界，不让桶伸出 data_window。

SQL 成功后，Go 按固定桶序列补零，保留 DEBUG/INFO/WARN/ERROR；遇到其他级别也保留原始计数，前端可合并为“其他”。只查聚合值，不读 attrs、日志正文或模板样本。

本接口采用半开区间。现有 Agent 日志接口使用 BETWEEN 的边界语义暂不改动；严格对账时说明二者可能在窗口末端恰好相等的时间戳上有差异，不将其误报成数据丢失。

### 3.4 新增 service-ts 索引

修改 `ops-agent-backend/internal/observability/model.go`，在 Service、Ts 字段上新增 `idx_svc_ts` 的 GORM 声明，保留现有索引。

当前 `ops-agent-backend/main.go` 已对 obs-mysql 的 LogEntry 执行 AutoMigrate。部署时确认该迁移成功，并通过 SHOW INDEX 检查实际索引；不能只看代码声明。必要时提供一次性手动建索引说明：

```sql
CREATE INDEX idx_svc_ts ON logs (service, ts);
```

该语句仅在确认索引不存在时执行，目标是 obs-mysql 的 observability.logs，不是业务 users 表。

使用实际数据的 EXPLAIN 检查按服务、时间过滤的查询计划，并记录耗时。不强制 FORCE INDEX，也不因为返回桶数小就认定扫描量小。第一版不同时新增 method/route 等多个组合索引。

## 4. 后台缓存生命周期

### 4.1 缓存键与内容

实时键：`service + method + route + rolling模式 + 15分钟窗口 + 10秒桶宽`。

历史键：`service + method + route + start_ms + end_ms + 桶宽`。

实时键不含不断变化的当前时间，避免每次请求创建新缓存。选择不同接口对应不同日志缓存；缓存只保存桶、汇总和状态，不保存原始日志。

### 4.2 刷新流程

1. 首次 Snapshot 创建条目、登记后台任务，立即返回 loading 状态，不等待 SQL。
2. 独立后台 worker 查询最近完整 15 分钟并聚合；历史模式查询指定固定范围。
3. 查询期间不持缓存全局锁。成功后一次性替换桶、汇总、数据窗口及成功时间。
4. 同一个条目已有排队/执行任务时不重复入队。
5. 实时条目仍被访问时，每次尝试间隔至少 15 秒；刷新可能因排队/查询耗时更晚完成。
6. 失败保留旧快照、记录 error，并按刷新间隔重试，不形成紧密重试循环。

15 秒是刷新间隔，不是“只缓存 15 秒数据”，也不是“满 15 秒删除缓存”。每次都重新聚合整个 15 分钟窗口，延迟入库且仍位于窗口中的日志可在后续刷新中补上。

第一版默认值：

| 项目 | 默认值 |
| --- | --- |
| 数据窗口 / 桶宽 | 15 分钟 / 10 秒 |
| 实时刷新尝试间隔 | 15 秒 |
| 历史快照成功后再次查询间隔 | 被访问时至少 60 秒，补充可能的延迟写入 |
| 无访问后停止自动刷新 | 1 分钟 |
| 无访问后淘汰 | 10 分钟 |
| 最大缓存条目 | 8 |
| SQL 超时 | 5 秒 |
| 日志后台查询并发 | 1 |

缓存满时淘汰最久未访问且不在加载的条目；全在加载时拒绝新条目。关闭进程时取消 SQL context，等待 worker 退出，再关闭数据库连接。

日志 worker 与 Trace worker 独立，避免慢 Jaeger 查询阻塞日志图。沿用现有数据库连接池，不为每份缓存新建连接池。第一版只复用生命周期思路，不为了统一而抽象一套复杂通用缓存框架。

## 5. 前端改动

### 5.1 布局和职责

顶栏入口从“Trace 面板”改为“观测面板”。页面结构：

```text
服务 / HTTP接口 / 实时或历史模式 / 时间范围 / 刷新
Trace 耗时散点图 + 独立加载状态与更新时间
日志级别数量 + 堆叠柱状图 + 独立加载状态与更新时间
共同选区预览 / 起止时间编辑 / 填入诊断范围 / 清除
当前对话输入框
```

前端继续使用现有 Canvas 和原生 JS，不增加图表库。

将 `trace-panel.mjs` 中的数据请求、画图、选区混合逻辑拆成三个职责：

- 面板控制器：共同筛选、显示时间窗口、轮询、请求代次、选区与草稿。
- Trace 渲染模块：散点、悬停详情，接收共同窗口和选区。
- 日志渲染模块：柱状图、级别图例、桶详情，接收同一窗口和选区。

实现时可保留现有 trace-panel 入口和 DOM ID，减少与 app.js 的无关改动；新增模块不要求为了命名重写整个面板。

### 5.2 两张图的数据获取与显示窗口

同一刷新轮次并发获取两份快照，独立处理成功/失败，一张图失败不阻塞另一张。首次未初始化期间可每 2 秒读取状态，已初始化后约每 15 秒轮询；隐藏页面或关闭面板停止轮询。

实时模式仍调用两个接口的实时缓存模式，不把每轮变化的 start_ms/end_ms 当成固定历史参数发送，否则会不断创建缓存并绕过实时后台刷新机制。

控制器每轮只计算一次共同 `displayWindow`，两图使用它绘制横轴；历史模式直接采用用户指定窗口。查询响应的 window/data_window 是各自的数据说明，不允许两张图各自选择横轴。

日志 data_window 之外的区间用淡色遮罩标为“尚未查询覆盖”，不补零。柱子在绘图区边缘可以裁切，但桶计数及 tooltip 始终属于它原来的完整返回区间，不按宽度比例推算计数。被显示窗口裁切的边界桶标注提示；顶部汇总标为“已查询窗口日志数”，显示实际 data_window，避免声称它精确属于更新后的 displayWindow。

Trace 也继续展示自己的 data_as_of_ms、stale、partial 等状态。共同时间轴表示方便对照，不代表两份数据在同一时刻已完整入库。

切换服务、接口、模式或历史窗口，取消旧请求，增加筛选代次，清理两张图的旧数据和选区；迟到响应不得覆盖新条件。日志和 Trace 各有自己的请求取消器。

服务目录第一版复用现有 Jaeger 目录并保留手动输入。目录暂不可用时仍允许输入日志服务进行查询，Trace 无数据/失败不能阻止日志显示；暂不新增全量日志服务发现接口。

### 5.3 日志图

- 横轴：共享时间；纵轴：每桶日志条数。
- 默认显示 DEBUG、INFO、WARN、ERROR，其他级别保留并显示为“其他”。
- 柱状图按级别堆叠，文字图例与颜色同时表达状态。
- 可通过图例隐藏 INFO 等级别，便于查看少量 WARN/ERROR；只是本地显示变化，不修改缓存和全级别总数。
- 悬停显示完整桶起止时间、每个级别数、总数；边界短桶标注实际长度。
- 首次查询失败、刷新失败保留旧数据、成功但零日志分别展示。

### 5.4 同步框选与诊断草稿

共享一份 `selection={start_ms,end_ms}`，不让两张图各自维护选区。

- 在任意图拖动时，两张图实时重绘同一选区；统一绘图区左右边距、横轴变换。
- 拖动期间冻结两张图的显示时间窗口，后台响应可暂存，松手后再应用；选区保持绝对时间。
- 完成拖动后，开始秒向下取整、结束秒向上取整，并让最终高亮与发送范围一致。
- 手动时间输入、选择整个窗口、清除选区同样更新两张图。
- 一次操作只更新一次草稿，保留用户其他文字；执行中不可修改草稿时保留选区并提示。

草稿使用统一的“观测诊断范围”块，内容包括服务、Unix 秒级起止时间；若选了 HTTP 接口，同时写明 Trace operation、日志 method/route。更新逻辑兼容替换现有 `[Trace 诊断范围]` 块，避免升级后出现两个范围块。

第一版不添加日志详情列表、模板弹窗或 Trace 瀑布图，先完成双图到诊断对话的联动。

## 6. 预计文件改动

| 模块 | 文件/位置 | 改动 |
| --- | --- | --- |
| ops-agent-backend | internal/observability/model.go | 新增 idx_svc_ts 声明 |
| obs-api | internal/logstore/visual_model.go（新） | 分桶查询、桶、汇总、响应模型 |
| obs-api | internal/logstore/histogram.go（新） | SQL 分桶、补零与汇总 |
| obs-api | internal/logstore/visual.go（新） | 有界内存缓存和后台 worker |
| obs-api | internal/handler/log_visual_handler.go（新） | 参数验证、读取快照 |
| obs-api | internal/router/router.go、main.go | 注入日志缓存、注册路由和关闭生命周期 |
| rag-gateway | router/router.go | 为 GET /api/v1/visual/logs 添加明确的只读代理路由 |
| rag-gateway | static/index.html、trace-panel.css | 新增日志区域、改面板标题、共用筛选和选区 |
| rag-gateway | static/trace-panel.mjs、trace-core.mjs | 提取面板共享状态，HTTP operation 映射、草稿兼容 |
| rag-gateway | static/trace-chart.mjs、log-chart.mjs（新，名称可按实际调整） | 两张图分别绘制，使用共享窗口/选区 |
| rag-gateway | tests/preview-server.mjs | 增加日志正常、空数据、失败和延迟快照示例 |
| 文档 | obs-api/docs/logs-visual.md 等 | 实际接口、索引部署、缓存及验收说明 |

现有 OBS_SERVICE_URL 和观测代理可复用，无需新增 Compose 服务或 Agent 工具。router 依赖注入的具体签名在实现时统一调整调用方和测试，不继续堆叠多个含义不清的可选参数。

## 7. 实施顺序与验收

1. 日志模型索引声明、分桶模型和 SQL；验证边界、补零、级别汇总、method/route 过滤。
2. 后台缓存和快照接口；验证合并重复刷新、失败保留旧数据、无人访问停止、淘汰、退出取消。
3. 网关只读路由、日志 Canvas 图和独立状态展示。
4. 双图共享筛选、坐标与选区，更新诊断草稿。
5. 本地模拟页面与测试，再由真实故障演练验证效果。

重点测试：

- 刚好位于桶边界、窗口 end_ms 的日志不会重复统计；首尾短桶正确。
- 具体 HTTP 接口筛选包含关联业务错误日志，排除其他接口和未匹配路由日志。
- 全部接口保留未匹配路由；未知级别计入总数且不丢失。
- 成功零日志与查询失败不混淆；旧快照不能把尚未查询到的时间段显示为零。
- 后台刷新时读快照不等待 SQL；同条件并发访问只排队一次。
- 任意一张图框选，两图高亮、手动输入和最终草稿时间一致。
- 实时刷新或服务切换期间，不发生选区漂移、旧响应覆盖新条件、重复范围块。
- Trace 请求失败时日志仍能显示，反之亦然；关闭面板与隐藏页面停止轮询。
- 保持现有 Agent SSE、历史对话和 Trace 工具行为。

真实验收使用 test.py 的正常、Redis 暂停、MySQL 行锁和 RabbitMQ 断线窗口：检查两图能否对照异常，框选后 Agent 是否能定位；图表自身不根据时间重合判定根因。

查询性能以实际 SQL 耗时、扫描行数和执行计划为依据。如果在当前数据规模下 15 分钟全窗口聚合仍明显耗时，再讨论覆盖索引、增量回查或持久化聚合表。
