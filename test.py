"""故障注入测试：制造有明确时间窗口的故障，用于验证观测系统能否捕捉到。

用法：python fault_injection.py
跑完会输出各阶段的时间戳，直接拿去查 obs-api 的 stats 接口。
"""
import json
import random
import subprocess
import time
import uuid
import requests

BASE = "http://localhost:8080/api/v1"
COMPOSE_DIR = "."          # docker-compose.yml 所在目录
TIMELINE = []


def compose(*args):
    """执行 docker compose 命令"""
    subprocess.run(["docker", "compose", *args], cwd=COMPOSE_DIR, check=True)


import uuid

def send_traffic(duration_sec: float, qps: float = 5):
    interval = 1.0 / qps
    end = time.time() + duration_sec
    stats = {"ok": 0, "err": 0}
    while time.time() < end:
        try:
            r = random.random()
            if r < 0.40:
                requests.get(f"{BASE}/users/{random.randint(1, 20)}", timeout=5)
            elif r < 0.55:
                requests.get(f"{BASE}/users?page=1&limit=10", timeout=5)
            elif r < 0.70:
                requests.put(f"{BASE}/users/{random.randint(1, 20)}",
                             json={"age": random.randint(18, 60)}, timeout=5)
            elif r < 0.88:
                # 创建用户：会走 bcrypt + MySQL 写入 + 布隆 + 发消息
                suffix = uuid.uuid4().hex[:8]
                requests.post(f"{BASE}/users", json={
                    "username": f"user_{suffix}",
                    "email": f"{suffix}@test.com",
                    "password": "test123456",
                    "age": random.randint(18, 60),
                }, timeout=5)
            elif r<0.91:
                # 测试用户名重复
                suffix = uuid.uuid4().hex[:8]
                requests.post(f"{BASE}/users", json={
                    "username": f"user_duplicate",
                    "email": f"{suffix}@test.com",
                    "password": "test123456",
                    "age": random.randint(18, 60),
                }, timeout=5)
            else:
                requests.get(f"{BASE}/users/{random.randint(99900, 99999)}", timeout=5)
            stats["ok"] += 1
        except Exception:
            stats["err"] += 1
        time.sleep(interval)
    return stats


def phase(name: str, duration: float, setup=None, teardown=None):
    """跑一个阶段，记录时间窗口"""
    if setup:
        setup()
    start = int(time.time())
    print(f"\n=== [{name}] 开始 ({duration}s) ===")
    stats = send_traffic(duration)
    end = int(time.time())
    if teardown:
        teardown()
    TIMELINE.append({"phase": name, "start": start, "end": end, **stats})
    print(f"=== [{name}] 结束  客户端视角: 成功 {stats['ok']} 失败 {stats['err']} ===")


def main():
    print("确保所有服务已启动：docker compose up -d")
    input("按回车开始...")

    # # 1. 基线：建立健康时的分母
    # phase("baseline", 60)

    # 2. Redis 无响应（pause）
    phase("redis_paused", 60,
          setup=lambda: compose("pause", "redis"),
          teardown=lambda: compose("unpause", "redis"))

    time.sleep(5)   # 给恢复留点时间

    # # 3. Redis 不可达（stop）
    # phase("redis_stopped", 60,
    #       setup=lambda: compose("stop", "redis"),
    #       teardown=lambda: (compose("start", "redis"), time.sleep(5)))

    # time.sleep(5)

    # # 4. MySQL 不可达（最惨烈：业务直接失败）
    # phase("mysql_stopped", 45,
    #       setup=lambda: compose("stop", "mysql"),
    #       teardown=lambda: (compose("start", "mysql"), time.sleep(15)))

    # time.sleep(15)

    # # 5. RabbitMQ 不可达（最隐蔽：业务全成功，但消息静默失败）
    # phase("rabbitmq_stopped", 60,
    #       setup=lambda: compose("stop", "rabbitmq"),
    #       teardown=lambda: (compose("start", "rabbitmq"), time.sleep(10)))

    # time.sleep(10)
    # # 6. 恢复：验证自愈
    # phase("recovered", 60)

    print("\n" + "=" * 60)
    print(json.dumps(TIMELINE, indent=2, ensure_ascii=False))
    print("=" * 60)
    for t in TIMELINE:
        print(f'curl "http://localhost:8082/api/v1/logs/stats'
              f'?start={t["start"]}&end={t["end"]}"   # {t["phase"]}')


if __name__ == "__main__":
    main()