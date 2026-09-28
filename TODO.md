# TODO

## obs-api（观测 API）

- [ ] **`/logs/templates` 截断无提示**：当模板数量超过 `limit` 时，`QueryTemplates` 的 SQL 用 `LIMIT ?` 会静默截断，既不往 `notices` 塞提示，`TemplatesResponse` 也没有 `has_more` 字段，调用方无从判断是否被截断。
  - 对比：`/logs/search` 有 `has_more` + `next_cursor`，`/traces/search` 有 `meta.has_more_matches`。
  - 修复方向：`QueryTemplates` 改成 `LIMIT ?+1` 多取一条判断是否还有更多，`TemplatesResponse` 加 `has_more` 字段（或塞一条 notice）。
