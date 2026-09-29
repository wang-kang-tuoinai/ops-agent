# TODO

## obs-api（观测 API）

- [x] **`/logs/templates` 截断提示**：`QueryTemplates` 使用 SQL `LIMIT ?`，绑定 `limit+1`，多取一组后裁剪到 limit；响应增加 `has_more`，截断时附带 notice。恰好 limit 组不误报截断，已通过 store/handler 测试。
  - service 改为必填，分组限定单个服务；无模板游标，可提高 limit（最多 500）或缩小筛选范围。
  - 详见 [日志模板接口](obs-api/docs/logs-templates.md)。
