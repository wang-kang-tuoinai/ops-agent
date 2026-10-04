# 本地故障演练

`test.py` 默认持续发送 30 分钟业务流量，基础调度 3 QPS、最多 8 个并发请求，
创建 24 个专用用户，加入 Redis 短暂停顿、MySQL 局部行锁等待、RabbitMQ 断线和异常请求模式。
准备用户和退出清理不包含在 30 分钟流量阶段内。只对你自己的测试环境运行。

## 运行

在 ops-agent 根目录、自己的 Python 环境中安装依赖：

```powershell
python -m pip install -r requirements-test.txt
python test.py --dry-run
python test.py
```

`--dry-run` 只打印计划，不需要第三方依赖，不连接 HTTP/MySQL/Docker，不写文件。
正式运行会修改测试用户数据，并短暂暂停 Redis、停止后启动 RabbitMQ。
脚本不会启动一套新环境；需要后端、观测组件和被测依赖已运行。
如果所选故障依赖原本已停止/暂停，预检查失败，先恢复环境再运行。

其他示例：

```powershell
# 5 分钟演练，时间窗口按比例缩放；短故障仍使用实际持有时长。
python test.py --duration 300 --qps 3 --seed 42
# 只发送正常流量，不调用 Docker、不连接 MySQL 注入行锁。
python test.py --duration 300 --faults none
# 暂不使用 MySQL 场景。
python test.py --faults redis,rabbitmq,patterns
```

`--duration` 单位为秒；`--users` 为准备用户数；`--max-requests` 默认 10000，
包含准备、正常流量、异常请求和行锁定向 PUT。预算耗尽会提前结束并清理。
行锁的定向 PUT 在基础 QPS 之外少量追加，但同样受并发和总请求预算限制。
线程忙时丢弃调度槽并记录 `capacity_skipped`，调度落后记录 `schedule_skipped`，不积压补发。
`--compose-dir` 默认脚本目录；`--base-url` 默认 `http://localhost:8080/api/v1`。
更换目标时必须确保 HTTP、Compose 和 MySQL 指向同一套测试环境。

## MySQL 连接

独立 Python 连接访问业务 MySQL，不是存日志的 obs-mysql。
默认匹配当前 Compose：`127.0.0.1:3306`、数据库 `ops_agent`、用户 `root`、密码 `root`。
可通过环境变量覆盖，脚本不会自动读取 `.env`，也不会将密码写入结果：

- `TEST_MYSQL_HOST` / `TEST_MYSQL_PORT`
- `TEST_MYSQL_USER` / `TEST_MYSQL_PASSWORD`
- `TEST_MYSQL_DATABASE`

行锁线程开启事务，以主键 `SELECT ... FOR UPDATE` 锁定本次创建的用户，校验用户名，
获得锁后才向独立 HTTP 工作线程提交同用户 PUT，持有约 300～800ms 后回滚。
无论请求是否完成，到时都会回滚释放；异常时关闭数据库连接。
获得锁失败、用户不匹配时不发送定向 PUT。
如果并发槽已满，记录 `target_skipped`；若 Redis 同时异常，PUT 也可能在加锁阶段就失败，
因此行锁注入成功不等于请求一定在 MySQL 等待。需要结合 Trace 核对。

## 默认场景与数据

- 0～3 分钟正常流量，之后穿插短暂 Redis 暂停和少量 MySQL 行锁等待。
- 9～12 分钟内安排一次 RabbitMQ 停止，持有约 15 秒后启动；容器运行不代表应用已重连。
  同时启用 MySQL 时，在这个计划窗口内安排一次行锁等待，观察跨组件异常重叠。
- 16～23 分钟穿插不存在 ID 查询、重复用户名提交、热点用户更新，与轻微故障重叠。
- 23～27 分钟为稀疏故障，最后 3 分钟不再注入新故障，继续正常请求观察恢复。

异常请求窗口中约 65% 的请求使用对应模式，总 QPS 不提升。热点更新只增加竞争机会，
默认低 QPS 下不保证发生锁冲突；这些模式也不能单凭请求结果证明真实恶意攻击。
同一组件不会并发执行多个故障；不同组件可以重叠。
随机种子固定计划和请求选择，线程调度、耗时及实际影响仍可能不同。

正常流量约为详情 GET 40%、列表 GET 20%、PUT 25%、POST 15%。
POST 会持续新增测试用户，结束后保留，便于复盘，不清空现有数据、不重置 ID。
准备用户的真实 ID 保存在清单中，不根据数据库记录总数猜 ID。
枚举场景使用大 ID 作为候选，只有真实返回 404 才判为符合预期。

## 结果与验收

结果位于 `test-results/<run_id>/`，已加入 Git 忽略：

| 文件 | 内容 |
| --- | --- |
| `manifest.json` | 参数、计划、实际准备用户 ID、流量开始时间 |
| `requests.jsonl` | 每次请求开始时间、耗时、接口、状态码、场景、可用的响应 Trace ID |
| `faults.jsonl` | 实际注入、恢复、失败、跳过，以及需要恢复的容器 ID |
| `summary.json` | 状态类别、预期结果、跳过数量、分接口耗时、未恢复组件 |

所有 `*_ms` 时间戳为 Unix 毫秒，工具需要 Unix 秒时除以 1000。
计划的 `at` 相对于 `traffic_started_at_ms`；实际事件用 `recorded_at_ms`。
`hold` 是注入状态确认后的等待时长，不包含 Docker 命令耗时。
以 `starting`、`injected`、`restored/released` 及请求记录核对实际影响窗口，不用计划代替事实。

HTTP 500 计入 5xx，客户端超时/连接失败单列。
重复注册预期 409，枚举预期 404，热点更新允许 2xx 或 409；其他场景预期 2xx。
`expected` 表示 HTTP 结果是否符合请求场景，不代表依赖健康，也不是 Agent 诊断评分。
准备与流量的接口耗时分别聚合；客户端耗时包含网络等开销，不等于根 Span 耗时。
不保存响应正文、请求密码或数据库密码。发送的测试请求头并不保证被后端记录。
不要将故障时间线给 Agent；先让它诊断，再用时间线和请求证据验收。

## 中止与清理

按 Ctrl+C 停止调度，唤醒故障线程释放行锁/恢复容器，再等待已有请求结束。
Docker 命令有 30 秒超时，恢复失败会重试并保留记录；清理可能因此持续数分钟。
退出码 0 表示脚本完成（业务 5xx 可以是演练结果），130 表示用户中止，1 表示执行或恢复出错。
不能保证强制杀进程、关闭终端或机器断电时清理成功。
若发生这种情况，先查看 `restore_registered` 的容器及动作，确认后手动恢复；
不要删除数据库记录或 Redis 锁来替代组件恢复。

## 脚本回归测试

```powershell
python -m unittest discover -s tests -p test_fault_runner.py -v
```

测试使用 HTTP、Docker 和 MySQL 替身，不接触真实服务。真实演练仍需你运行脚本验收。
