# 日志工具多服务改造方案

> 范围：obs-api 的三个日志观测接口 `/logs/stats`、`/logs/templates`、`/logs/search`，
> 以及诊断 agent 侧对应的三个工具 `query_log_stats`、`query_log_templates`、`search_logs`。
>
> 目标：在多服务场景下，让「不指定 service」时能正确按服务拆分返回，而不是把所有服务揉成一个结果。

## 1. 现状盘点：地基已经就位

好消息是，多服务需要的底层能力已经全部存在，缺的只是「拆分返回」这一层：

| 事实 | 位置 | 说明 |
|---|---|---|
| 每条日志都写了 service | `ops-agent-backend/internal/observability/model.go` | `logs` 表有 `Service varchar(64) not null` |
| 写入时填充 service | `ops-agent-backend/internal/observability/recorder.go` | `Record()` 里 `Service: r.service` |
| service 目前是写死的 | `ops-agent-backend/main.go` | `NewRecorder(obs_db, "ops-agent-backend")` |
| 已按 service 建索引 | `ops-agent-backend/internal/observability/model.go` | `idx_svc_level_ts = (service, level, ts)` |
| 三个接口已支持单 service 过滤 | `obs-api/internal/logstore/mysql.go` | 每个查询都有 `if q.Service != "" { service = ? }` |

结论：**「按某个 service 查」已经通了**，当前缺口是「不指定 service 时，把多个服务拆开返回」。

## 2. 逐工具改造方案

### 2.1 `search_logs`（`/logs/search`）—— 零改动

`LogItem` 已经带 `service` 字段（`obs-api/internal/logstore/log_model.go`），按 service 过滤也已支持。
多服务下直接可用。可选：更新 agent 工具 docstring，说明「不传 service 会返回所有服务的日志」。

### 2.2 `query_log_stats`（`/logs/stats`）—— 加 `by_service` 拆分

**现状**：不传 service 时，把所有服务揉成一个 `summary`（total / error_count / by_level / top_templates）。
代码里已留 TODO（`obs-api/internal/handler/log_handler.go` Stats 上方）：「多服务可能需要标明每个服务的错误数」。

**改法**：

1. 新增一条按 `(service, level)` 分组的查询：

   ```sql
   SELECT service, level, COUNT(*) FROM logs WHERE <conds> GROUP BY service, level
   ```

   该查询能直接命中现有索引 `idx_svc_level_ts = (service, level, ts)`。

2. 响应模型加 `by_service` 字段：

   ```jsonc
   {
     "summary": { /* 保持现状，整体聚合 */ },
     "by_service": [
       { "service": "ops-agent-backend", "total": 1200, "error_count": 12, "error_rate": 0.01 },
       { "service": "obs-api",          "total": 300,  "error_count": 1,  "error_rate": 0.003 }
     ],
     "generated_at": ...,
     "notices": [...]
   }
   ```

   其中每个 service 的 `error_rate = error_count / total`，口径与现有 `summary.error_rate` 一致。

3. 语义约定：
   - **不传 `service`**：`summary` 保持整体聚合（向后兼容），`by_service` 给出逐服务拆分。
   - **传了 `service`**：`by_service` 只含一项（或省略），`summary` 即为该服务的聚合。

### 2.3 `query_log_templates`（`/logs/templates`）—— 分组键补 service

**现状**：`TemplateStat` 没有 `service` 字段，SQL 是 `PARTITION BY template, level`
（`obs-api/internal/logstore/mysql.go` QueryTemplates）。后果：两个不同服务产生同一条模板
（如都报 `connection refused`）会被合并成一组，count 相加、`sample` 只显示其中一条，归因错乱。

**改法**：

1. 分组键加 service：窗口函数的 `PARTITION BY template, level` → `PARTITION BY service, template, level`，
   `ROW_NUMBER() OVER (PARTITION BY ...)` 同步改。
2. SELECT 与输出加 service：`TemplateStat` 和 `TemplateSample`（`obs-api/internal/logstore/log_model.go`）
   各加一个 `service` 字段。
3. 索引说明：`(service, template, level)` 目前没有完全匹配的索引，但现有
   `idx_svc_level_ts = (service, level, ts)` 可覆盖 service + level 的过滤，先不改索引，
   待数据量上来再看是否需要 `(service, template, ts)` 复合索引。

## 3. 既有坑（多服务下会放大，建议一并处理或至少标注）

1. **stats 的 `error_count` 双重计数**（`log_handler.go` Stats 上方 TODO）：
   access log 的 5xx 与业务 `HandleError` 记的 ERROR 会重复算。拆成 `by_service` 后，
   脏数据会分摊到每个 service，误导性更强。方案见该 TODO（加 `kind` 列 / 消重 / 维持现状靠模型推导）。

2. **templates 的 `sample` 只取最新一条**（`log_handler.go` Templates 上方 TODO）：
   一个模板跨多个路由/根因时，sample 会让人误以为它只发生在某一处。
   加 service 分组能缓解「跨服务」问题，但「同 service 内跨路由」仍在。

## 4. agent 工具层改动（`ops-diagnosis-agent/langgraph_tools.py`）

三个工具**都已经有 `service` 参数**，且都是把后端 JSON 原样返回，所以工具签名基本不用动，只改 docstring：

- `query_log_stats`：`service` 的说明从「不传查所有服务」改为「不传时按服务拆分返回（见 by_service）」，并补一句 `by_service` 的语义。
- `query_log_templates`：说明「不传 service 时按 (service, template, level) 分组，结果含 service 字段」。
- `search_logs`：说明「不传 service 会返回所有服务的日志行」。

## 5. 改动清单汇总

| 文件 | 改动 | 工作量 |
|---|---|---|
| `obs-api/internal/logstore/log_model.go` | `StatsSummary` 加 `ByService`；`TemplateStat`/`TemplateSample` 加 `Service` | 小 |
| `obs-api/internal/logstore/mysql.go` | Stats 加 `GROUP BY service, level` 查询；Templates 分组键加 service | 中 |
| `obs-api/internal/handler/log_handler.go` | Stats 组装 `by_service`；Templates 透传 service 字段 | 小 |
| `ops-diagnosis-agent/langgraph_tools.py` | 三个工具 docstring 同步 | 小 |
| `obs-api/internal/logstore/store.go` | 接口/模型如有需要同步（`StatsResult` 等） | 小 |

## 6. 验证方式

1. 不传 `service` 调 `/logs/stats`：确认响应出现 `by_service`，且各 service 的 total 之和 ≈ `summary.total`。
2. 传 `service=obs-api` 调 `/logs/stats`：确认 `by_service` 只含该项（或省略），`summary` 正确。
3. 造两条不同 service、同模板的日志，不传 service 调 `/logs/templates`：确认返回两条（按 service 分开），且各自带 service 字段。
4. 回归：传单一 `service` 时三个接口的行为与改造前一致。
