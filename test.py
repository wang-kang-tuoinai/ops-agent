"""本地运维演练。用法见 fault-testing.md；--dry-run 不连接服务或创建文件。"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import random
import signal
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Fault:
    name: str
    kind: str
    at: float
    hold: float
    target: int = 0


def build_plan(duration, seed, enabled):
    """按比例缩放触发窗口；短演练保留真实故障时长和末尾恢复区间。"""
    rng = random.Random(seed)
    scale = duration / 1800
    events = []
    # 起始，结束，组件，次数
    specs = [(180, 600, "redis", 7), (360, 780, "mysql", 12),
             (960, 1380, "redis", 4), (960, 1380, "mysql", 10),
             (1380, 1620, "redis", 2), (1380, 1620, "mysql", 4)]
    last = defaultdict(lambda: -100.0)
    for lo, hi, kind, count in specs:
        if kind not in enabled:
            continue
        for at in sorted(rng.uniform(lo, hi) * scale for _ in range(count)):
            hold = rng.uniform(.3, .8) if kind == "mysql" else rng.uniform(.8, 2.5)
            at = max(at, last[kind] + 5)
            if at + hold >= duration * .9:
                continue
            events.append(Fault(f"{kind}-{len(events)+1}", kind, round(at, 3),
                                round(hold, 3), rng.randrange(3)))
            last[kind] = at + hold
    if "rabbitmq" in enabled:
        at = rng.uniform(540, 720) * scale
        hold = min(15.0, duration*.04)
        events.append(Fault("rabbitmq-1", "rabbitmq", round(at, 3), hold))
        if "mysql" in enabled:
            # 明确安排一次跨组件重叠；实际影响仍以执行记录与请求证据为准。
            overlap = Fault("mysql-overlap", "mysql", round(at+hold/2, 3), .6)
            events = [e for e in events if e.kind != "mysql" or
                      e.at+e.hold+5 <= overlap.at or overlap.at+overlap.hold+5 <= e.at]
            events.append(overlap)
    if "patterns" in enabled:
        for i, kind in enumerate(("enumeration", "duplicate", "hot_update")):
            events.append(Fault(f"pattern-{i+1}", kind, (960+i*130)*scale, 100*scale))
    return sorted(events, key=lambda e: e.at)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--duration", type=float, default=1800, help="流量阶段秒数，默认 1800")
    p.add_argument("--qps", type=float, default=3)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--users", type=int, default=24)
    p.add_argument("--max-requests", type=int, default=10000, help="含准备用户、定向 PUT 的总预算")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--timeout", type=float, default=5)
    p.add_argument("--base-url", default="http://localhost:8080/api/v1")
    p.add_argument("--compose-dir", type=Path, default=ROOT)
    p.add_argument("--output", type=Path, default=ROOT / "test-results")
    p.add_argument("--faults", default="redis,mysql,rabbitmq,patterns", help="逗号分隔；none 仅正常流量")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    for name, lo, hi in (("duration", 30, 86400), ("qps", .1, 100),
                         ("workers", 1, 32), ("users", 3, 100), ("timeout", .1, 30)):
        v = getattr(a, name)
        if not math.isfinite(v) or not lo <= v <= hi:
            p.error(f"{name} 必须在 {lo}～{hi} 之间")
    if not a.users < a.max_requests <= 100000:
        p.error("max-requests 必须大于 users 且不超过 100000")
    a.enabled = set() if a.faults == "none" else set(a.faults.split(","))
    if not a.enabled <= {"redis", "mysql", "rabbitmq", "patterns"}:
        p.error("未知 faults 参数")
    parsed = urlsplit(a.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        p.error("base-url 必须是不带凭据的 HTTP(S) URL")
    a.base_url = a.base_url.rstrip("/")
    return a

# 统计请求以及故障注入事件，按请求阶段、方法、接口分组计算延迟分位数，并写入文件。
class Recorder:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self.lock = threading.Lock()
        self.files = {name: (directory / f"{name}.jsonl").open("w", encoding="utf-8")
                      for name in ("requests", "faults")}
        self.counts = Counter()
        self.groups = defaultdict(list)
        self.outcomes = defaultdict(Counter)

    def emit(self, stream, data):
        with self.lock:
            self.files[stream].write(json.dumps({"recorded_at_ms": time.time_ns()//1000000,
                                                **data}, ensure_ascii=False) + "\n")
            self.files[stream].flush()
            if stream == "requests":
                self.counts[data["outcome"]] += 1
                self.counts["expected" if data["expected"] else "unexpected"] += 1
                key = f'{data["phase"]} {data["method"]} {data["operation"]}'
                self.groups[key].append(data["duration_ms"])
                self.outcomes[key][data["outcome"]] += 1

    def count(self, name, n=1):
        with self.lock:
            self.counts[name] += n

    def save(self, name, data):
        (self.directory / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def finish(self, **extra):
        groups = {}
        for key, samples in self.groups.items():
            samples.sort()
            groups[key] = {"count": len(samples), "outcomes": dict(self.outcomes[key]),
                           "p50_ms": samples[math.ceil(len(samples)*.5)-1],
                           "p95_ms": samples[math.ceil(len(samples)*.95)-1], "max_ms": samples[-1]}
        self.save("summary.json", {**extra, "counts": dict(self.counts), "interfaces": groups})
        for f in self.files.values():
            f.close()


class Docker:
    def __init__(self, directory):
        self.directory = directory

    def run(self, *args):
        # 不输出 Compose 环境变量、命令 stderr 或数据库连接凭据。
        result = subprocess.run(["docker", *args], cwd=self.directory, capture_output=True,
                                text=True, timeout=30, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError(f"Docker 命令失败（退出码 {result.returncode}）")
        return result.stdout

    def container(self, service):
        ids = self.run("compose", "ps", "-a", "-q", service).split()
        if len(ids) != 1:
            raise RuntimeError(f"{service} 需要且只能有一个现存容器")
        return ids[0]

    def state(self, cid):
        return json.loads(self.run("inspect", "--format", "{{json .State}}", cid))


class Exercise:
    # plan是按 at 排序的 Fault 列表，用于调度故障注入，requests_module 是 requests 包，db_connect 是可选的 MySQL 连接函数。
    def __init__(self, args, rec, run_id, plan, requests_module, db_connect=None):
        self.a, self.rec, self.run_id, self.plan = args, rec, run_id, plan
        self.http, self.db_connect = requests_module, db_connect
        self.stop = threading.Event()
        self.slots = threading.BoundedSemaphore(args.workers)
        self.pool = ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="traffic")
        self.fault_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="fault")
        self.fault_slots = threading.BoundedSemaphore(3)
        self.docker = Docker(args.compose_dir)
        self.containers = {}
        self.pending_restore = {}
        self.resource_locks = {k: threading.Lock() for k in ("redis", "rabbitmq", "mysql")}
        self.guard = threading.Lock()
        self.sent = 0
        self.users = []
        self.started = None
        self.unrecovered = []

    def log_fault(self, event, state, **extra):
        self.rec.emit("faults", {"event": event.name, "kind": event.kind, "state": state, **extra})

    def preflight(self):
        for service in sorted(self.a.enabled & {"redis", "rabbitmq", "mysql"}):
            cid = self.docker.container(service)
            state = self.docker.state(cid)
            if not state.get("Running") or state.get("Paused"):
                raise RuntimeError(f"{service} 原本未运行或已暂停，请先恢复环境")
            self.containers[service] = cid
        if "mysql" in self.a.enabled:
            # 工厂函数，每次调用返回一个Mysql连接
            conn = self.db_connect()
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1 FROM users LIMIT 1")
            finally:
                conn.close()

    def payload(self, sequence):
        name = f"ops_{self.run_id}_{sequence}"
        return {"username": name, "email": f"{name}@example.com", "password": "test123456", "age": 25}

    def request(self, method, path, scenario="normal", body=None, user_id=None, phase="traffic", event=None):
        with self.guard:
            if self.sent >= self.a.max_requests:
                self.rec.count("budget_skipped")
                self.stop.set()
                return None
            self.sent += 1
            request_id = f"{self.run_id}-{self.sent}"
        start_ms, tick = time.time_ns()//1000000, time.monotonic()
        status, error, trace_id, data = None, None, None, None
        try:
            # 每次请求独立 Session，避免跨线程共享会话；不自动重试创建请求。
            with self.http.Session() as session:
                session.trust_env = False
                response = session.request(method, self.a.base_url + path, json=body,
                                           headers={"User-Agent": "ops-agent-local-exercise/1.0",
                                                    "X-Test-Run-ID": self.run_id,
                                                    "X-Test-Request-ID": request_id},
                                           timeout=(min(2, self.a.timeout), self.a.timeout),
                                           allow_redirects=False)
                status = response.status_code
                trace_id = response.headers.get("X-Trace-ID")
                if phase == "prepare" and 200 <= status < 300:
                    try:
                        data = response.json()
                    except ValueError:
                        error = "invalid_json"
        except self.http.exceptions.Timeout:
            error = "client_timeout"
        except self.http.exceptions.RequestException:
            error = "client_connection_error"
        outcome = f"{status//100}xx" if status is not None else error
        expected = (status == 404 if scenario == "enumeration" else
                    status == 409 if scenario == "duplicate" else
                    status is not None and (200 <= status < 300 or (scenario == "hot_update" and status == 409)))
        operation = "/users/:id" if user_id is not None else "/users"
        self.rec.emit("requests", {"request_id": request_id, "phase": phase, "scenario": scenario,
                                   "fault_event": event, "method": method, "operation": operation,
                                   "path": path, "user_id": user_id, "start_ms": start_ms,
                                   "duration_ms": round((time.monotonic()-tick)*1000, 3),
                                   "status": status, "error": error, "outcome": outcome,
                                   "expected": bool(expected and error is None), "trace_id": trace_id})
        return data
    # 准备用户并把创建的用户的Id以及Username保存到 manifest.json 中，供后续流量阶段使用。
    def prepare(self, manifest):
        for i in range(self.a.users):
            if self.stop.is_set():
                raise RuntimeError("准备阶段已停止")
            body = self.payload(f"seed{i}")
            data = self.request("POST", "/users", body=body, phase="prepare")
            if not isinstance(data, dict) or type(data.get("id")) is not int or data["id"] <= 0:
                raise RuntimeError("准备用户失败；请检查 requests.jsonl，不会自动重试 POST")
            self.users.append({"id": data["id"], "username": body["username"]})
            manifest["users"] = list(self.users)
            self.rec.save("manifest.json", manifest)

    def submit(self, *args, **kwargs):
        if self.stop.is_set() or not self.slots.acquire(blocking=False):
            self.rec.count("capacity_skipped")
            return False
        try:
            future = self.pool.submit(self.request, *args, **kwargs)
        except BaseException:
            self.slots.release()
            raise
        def finished(f):
            self.slots.release()
            if f.exception() is not None:
                self.rec.count("worker_errors")
                self.stop.set()
        future.add_done_callback(finished)
        return True

    def traffic(self, rng, index, elapsed):
        pattern = next((e.kind for e in self.plan if e.kind in {"enumeration", "duplicate", "hot_update"}
                        and e.at <= elapsed < e.at+e.hold), None)
        uid = rng.choice(self.users)["id"]
        if pattern and rng.random() < .65:
            if pattern == "enumeration":
                # 大 ID 只是候选；只有返回 404 才算符合预期，不假定其一定不存在。
                uid = 2_000_000_000 + index
                return self.submit("GET", f"/users/{uid}", pattern, user_id=uid)
            if pattern == "duplicate":
                body = self.payload(f"duplicate{index}")
                body["username"] = self.users[0]["username"]
                return self.submit("POST", "/users", pattern, body=body)
            uid = self.users[0]["id"]
            return self.submit("PUT", f"/users/{uid}", pattern, {"age": rng.randint(18, 60)}, uid)
        r = rng.random()
        if r < .4:
            self.submit("GET", f"/users/{uid}", user_id=uid)
        elif r < .6:
            self.submit("GET", "/users?page=1&limit=10")
        elif r < .85:
            self.submit("PUT", f"/users/{uid}", body={"age": rng.randint(18, 60)}, user_id=uid)
        else:
            self.submit("POST", "/users", body=self.payload(f"traffic{index}"))

    def restore(self, cid, action, event):
        for attempt in range(1, 4):
            try:
                state = self.docker.state(cid)
                if action == "unpause" and state.get("Paused"):
                    self.docker.run("unpause", cid)
                elif action == "start" and not state.get("Running"):
                    self.docker.run("start", cid)
                state = self.docker.state(cid)
                if not state.get("Running") or state.get("Paused"):
                    raise RuntimeError("容器未恢复到运行状态")
                with self.guard:
                    self.pending_restore.pop(cid, None)
                self.log_fault(event, "restored", note="容器已运行；应用重连/健康需结合请求验证")
                return True
            except Exception as exc:
                self.log_fault(event, "restore_failed", attempt=attempt, error=type(exc).__name__)
        return False

    def container_fault(self, event):
        cid = self.containers[event.kind]
        state = self.docker.state(cid)
        if not state.get("Running") or state.get("Paused"):
            self.log_fault(event, "skipped", reason="触发前容器状态发生变化")
            return
        action = "unpause" if event.kind == "redis" else "start"
        # 先登记恢复义务：命令超时也可能已在 Docker 服务端生效。
        with self.guard:
            self.pending_restore[cid] = (action, event)
        try:
            self.log_fault(event, "restore_registered", container_id=cid, restore_action=action)
            if event.kind == "redis":
                self.docker.run("pause", cid)
            else:
                self.docker.run("stop", "--time", "2", cid)
            state = self.docker.state(cid)
            if (event.kind == "redis" and not state.get("Paused")) or (
                    event.kind == "rabbitmq" and state.get("Running")):
                raise RuntimeError("故障状态未确认")
            self.log_fault(event, "injected", hold_seconds=event.hold)
            self.stop.wait(event.hold)
        finally:
            if not self.restore(cid, action, event):
                self.stop.set()

    def mysql_fault(self, event):
        uid = self.users[event.target]["id"]
        conn = self.db_connect()
        try:
            conn.begin()
            with conn.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout = 2")
                cursor.execute("SELECT id, username FROM users WHERE id=%s FOR UPDATE", (uid,))
                row = cursor.fetchone()
                if row is None or row[1] != self.users[event.target]["username"]:
                    raise RuntimeError("测试用户不存在或数据库目标与后端不一致")
            self.log_fault(event, "injected", user_id=uid, hold_seconds=event.hold)
            # 锁已获得后才提交；HTTP 在独立线程执行，持锁线程到时直接回滚。
            accepted = self.submit("PUT", f"/users/{uid}", "row_lock", {"age": 31}, uid, event=event.name)
            self.log_fault(event, "target_submitted" if accepted else "target_skipped", user_id=uid)
            self.stop.wait(event.hold)
        finally:
            try:
                conn.rollback()
                self.log_fault(event, "released", user_id=uid)
            finally:
                conn.close()

    def fault(self, event):
        lock = self.resource_locks[event.kind]
        acquired = lock.acquire(blocking=False)
        try:
            if not acquired or self.stop.is_set():
                self.log_fault(event, "skipped", reason="同组件故障仍在执行或已停止")
                return
            self.log_fault(event, "starting", scheduled_offset=event.at)
            if event.kind == "mysql":
                self.mysql_fault(event)
            else:
                self.container_fault(event)
        except Exception as exc:
            self.rec.count("fault_errors")
            self.log_fault(event, "failed", error=type(exc).__name__,
                           detail=str(exc) if type(exc) is RuntimeError else None)
        finally:
            if acquired:
                lock.release()
            self.fault_slots.release()

    def run(self):
        self.started = time.monotonic()
        rng = random.Random(self.a.seed + 1)
        next_request, index, event_index, progress = 0.0, 0, 0, 0.0
        while not self.stop.is_set():
            elapsed = time.monotonic() - self.started
            if elapsed >= self.a.duration:
                break
            while event_index < len(self.plan) and self.plan[event_index].at <= elapsed:
                event = self.plan[event_index]
                event_index += 1
                if event.kind in self.resource_locks:
                    if self.fault_slots.acquire(blocking=False):
                        try:
                            self.fault_pool.submit(self.fault, event)
                        except BaseException:
                            self.fault_slots.release()
                            raise
                    else:
                        self.log_fault(event, "skipped", reason="故障并发已满")
                else:
                    self.log_fault(event, "pattern_window", end_offset=event.at+event.hold,
                                   note="只改变请求分布，实际请求见 requests.jsonl")
            if elapsed >= next_request:
                # 落后的调度槽直接跳过，不在恢复后突发补发。
                missed = max(0, int((elapsed-next_request)*self.a.qps))
                if missed:
                    self.rec.count("schedule_skipped", missed)
                index += missed + 1
                next_request += (missed+1)/self.a.qps
                self.traffic(rng, index, elapsed)
            if elapsed >= progress:
                print(f"已运行 {elapsed:.0f}/{self.a.duration:.0f}s，已发请求 {self.sent}", flush=True)
                progress = elapsed + 30
            self.stop.wait(min(.05, max(.001, next_request-elapsed)))
        for event in self.plan[event_index:]:
            self.log_fault(event, "skipped", reason="演练已结束")

    def cleanup(self):
        self.stop.set()
        # 先唤醒持锁/暂停线程恢复组件，再等待有网络超时的请求完成。
        self.fault_pool.shutdown(wait=True)
        for cid, (action, event) in list(self.pending_restore.items()):
            self.restore(cid, action, event)
        self.pool.shutdown(wait=True)
        self.unrecovered = [event.kind for _, event in self.pending_restore.values()]

# 工厂函数，每次调用返回一个Mysql连接
def mysql_connector():
    try:
        import pymysql
    except ImportError as exc:
        raise RuntimeError("MySQL 场景需要安装 requirements-test.txt；或用 --faults 排除 mysql") from exc
    def connect():
        return pymysql.connect(host=os.getenv("TEST_MYSQL_HOST", "127.0.0.1"),
                               port=int(os.getenv("TEST_MYSQL_PORT", "3306")),
                               user=os.getenv("TEST_MYSQL_USER", "root"),
                               password=os.getenv("TEST_MYSQL_PASSWORD", "root"),
                               database=os.getenv("TEST_MYSQL_DATABASE", "ops_agent"),
                               connect_timeout=3, read_timeout=5, write_timeout=5,
                               autocommit=False)
    return connect


def main(argv=None):
    args = parse_args(argv)
    plan = build_plan(args.duration, args.seed, args.enabled)
    if args.dry_run:
        print(json.dumps({"duration_seconds": args.duration, "qps": args.qps,
                          "max_requests": args.max_requests, "seed": args.seed,
                          "faults": [asdict(e) for e in plan]}, ensure_ascii=False, indent=2))
        return 0
    try:
        import requests
        connect = mysql_connector() if "mysql" in args.enabled else None
    except (ImportError, RuntimeError) as exc:
        print(str(exc) if isinstance(exc, RuntimeError) else "请先安装 requirements-test.txt")
        return 1
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    rec = Recorder(args.output / run_id)
    exercise = Exercise(args, rec, run_id, plan, requests, connect)
    manifest = {"run_id": run_id, "base_url": args.base_url, "duration_seconds": args.duration,
                "qps": args.qps, "workers": args.workers, "max_requests": args.max_requests,
                "timeout_seconds": args.timeout, "seed": args.seed, "users": [],
                "plan": [asdict(e) for e in plan], "started_at_ms": time.time_ns()//1000000}
    rec.save("manifest.json", manifest)
    interrupted = False
    def on_interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True
        exercise.stop.set()
        print("正在停止并恢复本次故障，请等待清理完成。", flush=True)
    old_handlers = {sig: signal.signal(sig, on_interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    exit_code = 0
    try:
        print(f"结果目录：{rec.directory}", flush=True)
        exercise.preflight()
        exercise.prepare(manifest)
        manifest["traffic_started_at_ms"] = time.time_ns()//1000000
        rec.save("manifest.json", manifest)
        exercise.run()
    except Exception as exc:
        exit_code = 1
        detail = str(exc) if type(exc) is RuntimeError else None
        rec.emit("faults", {"state": "run_failed", "error": type(exc).__name__, "detail": detail})
        print(f"演练停止：{detail or type(exc).__name__}。请检查结果文件及服务状态。", flush=True)
    finally:
        exercise.cleanup()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        has_errors = exercise.unrecovered or rec.counts["worker_errors"] or rec.counts["fault_errors"]
        exit_code = 1 if has_errors else 130 if interrupted else exit_code
        rec.finish(run_id=run_id, ended_at_ms=time.time_ns()//1000000, exit_code=exit_code,
                   interrupted=interrupted, unrecovered_components=exercise.unrecovered,
                   requests_attempted=exercise.sent, budget_exhausted=exercise.sent >= args.max_requests,
                   note="HTTP 成功不代表依赖无异常；注入记录不代表请求一定受影响。测试用户保留。")
        if exercise.unrecovered:
            print(f"需要人工恢复组件：{', '.join(exercise.unrecovered)}", flush=True)
        print(f"已保存：{rec.directory / 'summary.json'}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
