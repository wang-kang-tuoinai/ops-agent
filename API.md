# RAG 网关接口文档

网关服务 `rag-gateway`，对外监听 **`8081`** 端口，所有接口统一挂在 **`/api/v1`** 前缀下，请求/响应均为 **JSON**（`Content-Type: application/json`）。

网关仅做转发/组装，真正的问答与检索逻辑在下游 `rag-service`。请求经网关时会被 OpenTelemetry 埋点（服务名 `rag-bot`），可在 Jaeger UI（`http://localhost:16686`）查看链路。

- 基础地址：`http://<host>:8081/api/v1`
- 下游超时：网关到 `rag-service` 的 HTTP 客户端超时为 **30s**

---

## 目录

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | [/history](#1-获取会话列表) | 分页获取会话列表 |
| POST | [/ask](#2-发起提问不指定会话) | 发起新提问（自动新建会话） |
| POST | [/conversations/:conversation_id/ask](#3-在指定会话中提问) | 在已有会话中继续提问 |

---

## 通用约定

### 时间戳
`created_at` / `updated_at` 均为 **秒级 Unix 时间戳**（`int64`）。

### 分页（游标分页）
历史列表采用游标分页，不提供 `page`：

- 请求：传 `limit` 与上一页返回的 `next_cursor`
- 响应：`next_cursor` 为下一页游标；**没有更多数据时为 `null`**
- 终止条件：`has_more == false` 或 `next_cursor == null`，以先到者为准

### 错误响应
所有接口出错时均返回：

```json
{ "error": "<错误描述>" }
```

| HTTP 状态码 | 含义 | 典型触发 |
|-------------|------|---------|
| `400` | 参数错误 | `question` 为空、请求体非法 |
| `404` | 资源不存在 | `conversation_id` 对应的会话不存在 |
| `500` | 服务器内部错误 | 下游 `rag-service` 异常/超时等 |

---

## 1. 获取会话列表

`GET /history`

获取历史会话的摘要列表，按 `updated_at` 倒序。

### Query 参数

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `limit` | int | 否 | `20` | 每页条数。非法值或 `<1` 时取 `20`；**上限 `100`**，超过按 `100` 计 |
| `cursor` | int64 | 否 | 无 | 游标，取上一次响应中的 `next_cursor`。非正数或非法值将被忽略（等价于第一页） |

### 成功响应 `200 OK`

```json
{
  "items": [
    {
      "id": "e6adf03d1fd64562",
      "title": "Go接口命名规范",
      "created_at": 1787144026,
      "updated_at": 1787144038
    },
    {
      "id": "04a0fe00f34042b8",
      "title": "Go语言iota是什么",
      "created_at": 1787136169,
      "updated_at": 1787136323
    }
  ],
  "next_cursor": 1787121987,
  "has_more": true
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `items[].id` | string | 会话 ID |
| `items[].title` | string \| null | 会话标题，**可能为 `null`** |
| `items[].created_at` | int64 | 创建时间（秒级） |
| `items[].updated_at` | int64 | 最近更新时间（秒级） |
| `next_cursor` | int64 \| null | 下一页游标；无更多时为 `null` |
| `has_more` | bool | 是否还有更多数据 |

### 翻页示例

```
# 第一页
GET /api/v1/history?limit=20

# 下一页（cursor 取上一页响应的 next_cursor）
GET /api/v1/history?limit=20&cursor=1787121987
```

---

## 2. 发起提问（不指定会话）

`POST /ask`

发起一次 RAG 问答。**不带** `conversation_id` 时，网关会自动走「新建会话」分支，返回的响应里带新建的 `conversation_id`。

### 请求体

```json
{
  "question": "Go接口命名规范是什么？"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `question` | string | ✅ | 问题内容。**不能为空**，否则返回 `400` |

### 成功响应 `200 OK`

```json
{
  "answer": "根据资料，Go 中接口命名的约定是：只包含一个方法的接口应当以该方法的名称加上 `-er` 后缀来命名……",
  "conversation_id": "e9deed875b9645f4",
  "references": [
    {
      "source": "go-official\\effective_go\\names.md",
      "topic": "effective_go"
    },
    {
      "source": "go-official\\effective_go\\names.md",
      "topic": "effective_go"
    }
  ]
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `answer` | string | 模型生成的回答正文 |
| `conversation_id` | string | 本次新建/使用的会话 ID（**下次续聊请用它**） |
| `references[].source` | string | 引用来源的文档路径（Windows 风格反斜杠路径） |
| `references[].topic` | string | 引用的文档主题 |

---

## 3. 在指定会话中提问

`POST /conversations/:conversation_id/ask`

在已有会话中继续提问。请求体与 `/ask` 完全一致。

### 路径参数

| 参数 | 说明 |
|------|------|
| `conversation_id` | 会话 ID，即 `/ask` 或 `/history` 返回的 `id` |

### 请求体

```json
{
  "question": "继续讲一下 iota 的用法"
}
```

### 成功响应 `200 OK`

响应结构同 [2. 发起提问](#2-发起提问不指定会话)：

```json
{
  "answer": "根据资料，`iota` 的用法是在 `const` 声明块中作为枚举器……",
  "conversation_id": "04a0fe00f34042b8",
  "references": [
    {
      "source": "go-official\\effective_go\\initialization.md",
      "topic": "effective_go"
    }
  ]
}
```

### 错误响应

| 状态码 | 响应体 | 说明 |
|--------|--------|------|
| `404` | `{"error": "<下游 detail>"}` | 会话不存在（网关将下游 `rag-service` 的 404 原样透传） |
| `400` | `{"error": "question 不能为空"}` | 缺少 `question` |
| `500` | `{"error": "服务器内部错误"}` | 其他内部错误 |

---

## 调用流程（客户端建议）

```
[前端]
  │ GET  /api/v1/history            → 渲染会话列表
  │ POST /api/v1/ask                → 新会话提问（拿到 conversation_id）
  │ POST /api/v1/conversations/:id/ask → 续聊（传上一轮的 conversation_id）
  ▼
[rag-gateway:8081]  →  转发  →  [rag-service:8000]
```

## 示例

### curl

```bash
# 获取历史
curl "http://localhost:8081/api/v1/history?limit=20"

# 新提问
curl -X POST http://localhost:8081/api/v1/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Go接口命名规范是什么？"}'

# 续聊
curl -X POST http://localhost:8081/api/v1/conversations/04a0fe00f34042b8/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "继续讲一下 iota"}'
```

### PowerShell

```powershell
Invoke-RestMethod -Method Get -Uri "http://localhost:8081/api/v1/history?limit=20"
Invoke-RestMethod -Method Post -Uri "http://localhost:8081/api/v1/ask" `
  -ContentType "application/json" `
  -Body '{"question":"Go接口命名规范是什么？"}'
```

---

## 附：与上游 `rag-service` 的对应关系

网关是薄转发层，以下是内部对应关系，便于排查问题时直接对齐：

| 网关接口 | 上游 `rag-service` 路径 |
|---------|------------------------|
| `GET /api/v1/history` | `GET /api/v1/history/` |
| `POST /api/v1/ask` | `POST /api/v1/ask` |
| `POST /api/v1/conversations/:id/ask` | `POST /api/v1/conversations/:id/ask` |

- 上游 `rag-service` 对 `/ask` 系列的请求体也是 `{"question": "..."}`。
- 网关在「会话不存在」时收到上游 `404`，会透传成网关的 `404` 并携带上游的 `detail` 信息。
