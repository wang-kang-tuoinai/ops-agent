"""只使用内存中的 HTTP、Docker、数据库替身，不操作真实服务。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

import test as runner


class RequestError(Exception):
    pass


class Timeout(RequestError):
    pass


class FakeHTTP:
    exceptions = types.SimpleNamespace(Timeout=Timeout, RequestException=RequestError)

    def __init__(self, status=200, error=None):
        self.status, self.error = status, error
        self.ids = [3344, 3351, 3380]
        self.calls = []

    @contextlib.contextmanager
    def Session(self):
        yield self

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        data = {"id": self.ids[len(self.calls)-1]} if len(self.calls) <= 3 else {}
        return types.SimpleNamespace(status_code=self.status, headers={"X-Trace-ID": "trace-test"},
                                     json=lambda: data)


class FakeDocker:
    def __init__(self, pause_timeout=False, restore_fails=False):
        self.current = {"Running": True, "Paused": False}
        self.calls = []
        self.pause_timeout, self.restore_fails = pause_timeout, restore_fails

    def state(self, cid):
        return dict(self.current)

    def container(self, service):
        return service + "-id"

    def run(self, *args):
        self.calls.append(args)
        if args[0] == "pause":
            self.current["Paused"] = True
            if self.pause_timeout:
                raise TimeoutError("Docker timeout after effect")
        elif args[0] == "unpause":
            if self.restore_fails:
                raise TimeoutError()
            self.current["Paused"] = False
        elif args[0] == "stop":
            self.current["Running"] = False
        elif args[0] == "start":
            self.current["Running"] = True


class FakeDB:
    def __init__(self, row=(3344, "user-a"), fails=False):
        self.row, self.fails = row, fails
        self.calls = []

    def begin(self):
        self.calls.append("begin")

    @contextlib.contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "FOR UPDATE" in sql and self.fails:
            raise TimeoutError()

    def fetchone(self):
        return self.row

    def rollback(self):
        self.calls.append("rollback")

    def close(self):
        self.calls.append("close")


class FaultRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rec = runner.Recorder(Path(self.temp.name) / "run")
        args = runner.parse_args(["--users", "3", "--workers", "2", "--faults", "none"])
        self.http = FakeHTTP()
        self.ex = runner.Exercise(args, self.rec, "unit", [], self.http)
        self.ex.users = [{"id": 3344, "username": "user-a"},
                         {"id": 3351, "username": "user-b"}, {"id": 3380, "username": "user-c"}]
        self.addCleanup(self.finish)

    def finish(self):
        self.ex.cleanup()
        self.rec.finish()

    def records(self, name):
        return [json.loads(line) for line in (self.rec.directory / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_plan_reproducible_and_same_component_separated(self):
        enabled = {"redis", "mysql", "rabbitmq", "patterns"}
        for duration in (30, 300, 1800):
            plan = runner.build_plan(duration, 42, enabled)
            self.assertEqual(plan, runner.build_plan(duration, 42, enabled))
            for kind in ("redis", "mysql"):
                events = [e for e in plan if e.kind == kind]
                for before, after in zip(events, events[1:]):
                    self.assertGreaterEqual(after.at, before.at+before.hold+4.99)
            self.assertTrue(all(0 < e.at < e.at+e.hold <= duration*.9 for e in plan))
            rabbit = next(e for e in plan if e.kind == "rabbitmq")
            overlap = next(e for e in plan if e.name == "mysql-overlap")
            self.assertLess(rabbit.at, overlap.at)
            self.assertLess(overlap.at, rabbit.at+rabbit.hold)

    def test_dry_run_has_no_side_effects(self):
        with patch.object(runner, "Recorder") as rec, patch.object(runner, "Docker") as docker:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(["--dry-run"]), 0)
            rec.assert_not_called()
            docker.assert_not_called()

    def test_invalid_limits_rejected(self):
        for args in (["--qps", "nan"], ["--workers", "0"], ["--faults", "typo"],
                     ["--max-requests", "2"], ["--duration", "inf"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                runner.parse_args(args)

    def test_http_errors_are_not_success_and_business_expectation_separate(self):
        self.http.status = 500
        self.ex.request("PUT", "/users/3344", user_id=3344)
        self.http.status = 409
        self.ex.request("POST", "/users", scenario="duplicate")
        self.http.status = 404
        self.ex.request("GET", "/users/999", scenario="enumeration", user_id=999)
        rows = self.records("requests")
        self.assertEqual([r["outcome"] for r in rows], ["5xx", "4xx", "4xx"])
        self.assertEqual([r["expected"] for r in rows], [False, True, True])
        self.assertEqual(rows[0]["trace_id"], "trace-test")

    def test_timeout_has_separate_outcome(self):
        self.http.error = Timeout()
        self.ex.request("GET", "/users")
        self.assertEqual(self.records("requests")[0]["outcome"], "client_timeout")

    def test_prepare_retains_actual_non_contiguous_ids(self):
        self.ex.users = []
        self.ex.prepare({})
        self.assertEqual([u["id"] for u in self.ex.users], [3344, 3351, 3380])
        self.assertEqual(len(json.loads((self.rec.directory / "manifest.json").read_text(encoding="utf-8"))["users"]), 3)

    def test_budget_includes_all_requests(self):
        self.ex.a.max_requests = 2
        for _ in range(5):
            self.ex.request("GET", "/users")
        self.assertEqual(len(self.http.calls), 2)
        self.assertTrue(self.ex.stop.is_set())

    def test_capacity_does_not_queue_unbounded_requests(self):
        entered = threading.Event()
        release = threading.Event()
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(2)
        with patch.object(self.ex, "request", side_effect=blocked):
            try:
                self.assertTrue(self.ex.submit("GET", "/users"))
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.ex.submit("GET", "/users"))
                self.assertFalse(self.ex.submit("GET", "/users"))
                self.assertEqual(self.rec.counts["capacity_skipped"], 1)
            finally:
                release.set()
                self.ex.pool.shutdown(wait=True)

    def test_docker_timeout_still_recovers(self):
        self.ex.docker = FakeDocker(pause_timeout=True)
        self.ex.containers["redis"] = "redis-id"
        with self.assertRaises(TimeoutError):
            self.ex.container_fault(runner.Fault("r", "redis", 0, 0))
        self.assertFalse(self.ex.docker.current["Paused"])
        self.assertEqual(self.ex.pending_restore, {})
        self.assertIn("restore_registered", [r["state"] for r in self.records("faults")])

    def test_recovery_failure_is_retained_for_cleanup_retry(self):
        self.ex.docker = FakeDocker(restore_fails=True)
        self.ex.containers["redis"] = "redis-id"
        self.ex.container_fault(runner.Fault("r", "redis", 0, 0))
        self.assertTrue(self.ex.stop.is_set())
        self.assertIn("redis-id", self.ex.pending_restore)
        self.ex.docker.restore_fails = False
        self.ex.cleanup()
        self.assertEqual(self.ex.unrecovered, [])
        self.assertFalse(self.ex.docker.current["Paused"])

    def test_originally_paused_container_not_touched(self):
        self.ex.docker = FakeDocker()
        self.ex.docker.current["Paused"] = True
        self.ex.containers["redis"] = "redis-id"
        self.ex.container_fault(runner.Fault("r", "redis", 0, 0))
        self.assertEqual(self.ex.docker.calls, [])
        self.assertEqual(self.ex.pending_restore, {})

    def test_preflight_stopped_dependency_is_not_started(self):
        self.ex.a.enabled = {"rabbitmq"}
        self.ex.docker = FakeDocker()
        self.ex.docker.current["Running"] = False
        with self.assertRaises(RuntimeError):
            self.ex.preflight()
        self.assertEqual(self.ex.docker.calls, [])

    def test_rabbitmq_restores_original_container(self):
        self.ex.docker = FakeDocker()
        self.ex.containers["rabbitmq"] = "rabbitmq-id"
        self.ex.container_fault(runner.Fault("mq", "rabbitmq", 0, 0))
        self.assertEqual(self.ex.docker.calls, [("stop", "--time", "2", "rabbitmq-id"),
                                               ("start", "rabbitmq-id")])
        self.assertEqual(self.ex.pending_restore, {})

    def test_mysql_connection_closed_even_if_rollback_fails(self):
        db = FakeDB()
        self.ex.db_connect = lambda: db
        with patch.object(db, "rollback", side_effect=TimeoutError()), patch.object(self.ex, "submit"), self.assertRaises(TimeoutError):
            self.ex.mysql_fault(runner.Fault("m", "mysql", 0, 0))
        self.assertEqual(db.calls[-1], "close")

    def test_mysql_lock_then_submit_then_rollback(self):
        db = FakeDB()
        self.ex.db_connect = lambda: db
        def submitted(*args, **kwargs):
            self.assertTrue(any(isinstance(c, tuple) and "FOR UPDATE" in c[0] for c in db.calls))
            self.assertNotIn("rollback", db.calls)
            self.assertEqual(args[1], "/users/3344")
            return True
        with patch.object(self.ex, "submit", side_effect=submitted):
            self.ex.mysql_fault(runner.Fault("m", "mysql", 0, 0))
        self.assertEqual(db.calls[-2:], ["rollback", "close"])

    def test_mysql_failure_or_wrong_user_never_submits_put(self):
        for db in (FakeDB(fails=True), FakeDB(row=None), FakeDB(row=(3344, "another-user"))):
            self.ex.db_connect = lambda: db
            with patch.object(self.ex, "submit") as submit, self.assertRaises(Exception):
                self.ex.mysql_fault(runner.Fault("m", "mysql", 0, 0))
            submit.assert_not_called()
            self.assertEqual(db.calls[-2:], ["rollback", "close"])

    def test_stop_releases_long_hold_early(self):
        self.ex.docker = FakeDocker()
        self.ex.containers["redis"] = "redis-id"
        injected = threading.Event()
        original = self.ex.log_fault
        def logged(event, state, **extra):
            original(event, state, **extra)
            if state == "injected":
                injected.set()
        with patch.object(self.ex, "log_fault", side_effect=logged):
            thread = threading.Thread(target=self.ex.container_fault, args=(runner.Fault("r", "redis", 0, 30),))
            thread.start()
            try:
                self.assertTrue(injected.wait(1))
            finally:
                self.ex.stop.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertFalse(self.ex.docker.current["Paused"])

    def test_short_scheduler_with_mock_services(self):
        self.ex.a.duration = .16
        self.ex.a.qps = 30
        self.ex.plan = [runner.Fault("r", "redis", .01, .01)]
        self.ex.docker = FakeDocker()
        self.ex.containers["redis"] = "redis-id"
        with contextlib.redirect_stdout(io.StringIO()):
            self.ex.run()
        self.ex.cleanup()
        self.assertGreater(len(self.records("requests")), 0)
        self.assertIn("restored", [r["state"] for r in self.records("faults")])


if __name__ == "__main__":
    unittest.main()
