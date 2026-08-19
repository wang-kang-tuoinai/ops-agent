# ops-agent

RAG 智能运维助手。Go 网关（`rag-gateway`）+ Python RAG 服务（`rag-service`）+ Go 后端（`ops-agent-backend`），通过 Docker Compose 编排，并用 Jaeger + OpenTelemetry 做链路追踪。

根仓库只负责编排，**三个服务的代码存放在独立仓库里**，通过 git submodules 挂载。

## 仓库结构

| 目录 | 说明 | GitHub 仓库 |
|------|------|-------------|
| `ops-agent-backend/` | 后端服务 + consumer | [wang-kang-tuoinai/ops-agent-backend](https://github.com/wang-kang-tuoinai/ops-agent-backend) |
| `rag-gateway/` | Go 网关（对外的 HTTP 入口） | [wang-kang-tuoinai/rag-bot-client](https://github.com/wang-kang-tuoinai/rag-bot-client) |
| `rag-service/` | Python RAG 服务（模型 + 向量库） | [wang-kang-tuoinai/rag-bot](https://github.com/wang-kang-tuoinai/rag-bot) |

根仓库自身的文件只有 `docker-compose.yml`、`.gitmodules`、`README.md`；三个子目录在根仓库里只是「指针」，各自锁定到某个 commit。

## 前置要求

- Docker + Docker Compose（Compose v2）
- Git
- 环境变量 `DEEPSEEK_API_KEY`（`rag-service` 调用 DeepSeek 需要）

## 首次启动

```bash
# 1. 克隆根仓库并拉齐三个子模块（--recurse-submodules 一步到位）
git clone --recurse-submodules git@github.com:wang-kang-tuoinai/ops-agent.git
cd ops-agent

# 如果之前 clone 时没带 --recurse-submodules，用这条补齐子模块
git submodule update --init --recursive

# 2. 配置 DeepSeek key（两种方式任选其一）
#    方式 A：写进 .env 文件（推荐，compose 会自动读取）
echo "DEEPSEEK_API_KEY=sk-xxx" > .env
#    方式 B：临时注入环境变量（PowerShell）
#    $env:DEEPSEEK_API_KEY = "sk-xxx"

# 3. 构建并启动
docker compose up -d --build
```

> 首次运行 `rag-service` 会从 Hugging Face 下载模型（已挂载本地缓存可复用）。若缓存为空，冷启动会较慢，请耐心等待，或先单独 `docker compose up -d rag-service` 拉好模型缓存。

## 服务与端口

| 服务 | 端口 | 说明 |
|------|------|------|
| `rag-gateway` | 8081 | 对外 HTTP 网关（`/api/v1/ask`、`/api/v1/history` 等） |
| `app` | 8080 | 后端服务 |
| `jaeger` | 16686 | Jaeger UI（浏览器访问） |
| `jaeger` | 4318 | OTLP HTTP 接收端口（各服务上报 trace） |
| `rabbitmq` | 15672 | RabbitMQ 管理界面 |
| `mysql` | 3306 | MySQL（root/root，库 `ops_agent`） |
| `redis` | 6379 | Redis |

## 启动依赖顺序

```
rag-gateway ──等 rag-service 健康──> rag-service ──等 app──> app ──等 mysql/redis/rabbitmq/jaeger
```

- `rag-gateway` 通过 healthcheck 等 `rag-service` **模型加载完成**（`/health` 返回 `model_loaded: true`）后才启动，避免网关先于模型就绪。
- `rag-service` 启动时在 `lifespan` 里加载 embedding/reranker 模型和 ChromaDB。

## 修改子模块代码的提交流程

子模块代码要提交**两处**：先子仓库、后根仓库，且**必须先 push 子仓库再 push 根仓库**（否则根仓库指针指向的 commit 在远程不存在）。

```bash
# 1. 进子仓库提交代码并 push
cd rag-service
git add .
git commit -m "fix: 修复 xxx"
git push
cd ..

# 2. 回根目录提交「指针」变化并 push
git add rag-service
git commit -m "chore: 更新 rag-service"
git push
```

根目录 `git status` 的两种提示含义：

| 提示 | 含义 | 操作 |
|------|------|------|
| `rag-service (new commits)` | 子仓库已 commit，指针没跟上 | 回根目录提交指针 |
| `rag-service (modified content)` | 子仓库还有未提交改动 | 先进子仓库 commit |

## 注意事项

- **HF 缓存路径是 Windows 专属**：`docker-compose.yml` 里 `rag-service` 挂载了 `C:/Users/HP/.cache/huggingface`，换机器或换人需改成对应路径（或改成 `${HF_HOME}` 之类的变量）。
- **`.env` 已被忽略**：本仓库 `.gitignore` 忽略了 `.claude/`；`.env` 若含密钥请勿提交（当前未加入 `.gitignore`，如使用 `.env` 建议自行加入）。
