"""infra 自检：协议、指令、去重、限流、重试、HTTP 基座。

运行：python -m unittest discover -s tests -v   （工作目录 <仓库根>/shared）
"""
from __future__ import annotations

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1]
PROJECT = Path(__file__).resolve().parents[2]
PLUGINS = PROJECT / "plugins" / "commands"

sys.path.insert(0, str(SHARED))
sys.path.insert(0, str(PLUGINS))

from infra import (  # noqa: E402
    AT_ALL_ID,
    CommandContext,
    CommandRegistry,
    Dedupe,
    EnvelopeError,
    RateLimiter,
    RetryExhausted,
    RetryPolicy,
    DeadLetter,
    at_all,
    bot_mentioned,
    build_envelope,
    build_space_event,
    clean_text_for_bot,
    is_space,
    is_valid,
    make_trace_id,
    ok,
    parse_command,
    serve_in_thread,
    to_json,
    validate,
)
from loader import load_all  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TestEnvelope(unittest.TestCase):
    def test_qq_envelope_normal(self):
        env = build_envelope(
            "qq",
            chat_id="123456",
            chat_type="group",
            chat_name="测试群",
            sender_id="10001",
            sender_name="小明",
            mentions=[{"id": "20002", "name": "小飞鱼"}],
            text="@小飞鱼 在吗",
            raw={"message_id": "abc-1"},
            bot_ids=["20002"],
        )
        self.assertTrue(is_valid(env))
        self.assertTrue(bot_mentioned(env))
        self.assertEqual(env["mentions"][0]["ambiguous"], False)
        self.assertEqual(clean_text_for_bot(env), "在吗")

    def test_wechat_mention_is_ambiguous(self):
        env = build_envelope(
            "wechat",
            chat_id="filehelper",
            chat_type="private",
            sender_name="老王",
            mentions=["小飞鱼"],
            text="@小飞鱼 帮我看看",
            bot_names=["小飞鱼"],
        )
        m = env["mentions"][0]
        self.assertEqual(m["name"], "小飞鱼")
        self.assertTrue(m["ambiguous"])
        self.assertEqual(m["id"], "")
        self.assertTrue(m["is_bot"])

    def test_at_all(self):
        env = build_envelope(
            "qq",
            chat_id="1",
            sender_name="小明",
            mentions=[{"name": "所有人"}],
            text="@所有人 开会",
        )
        self.assertTrue(at_all(env))
        # @所有人 不带具体 id，靠 is_all 区分
        self.assertEqual(env["mentions"][0]["id"], "")
        self.assertTrue(env["mentions"][0]["is_all"])

    def test_at_all_by_sentinel_id(self):
        env = build_envelope(
            "wechat",
            chat_id="1",
            sender_name="小明",
            mentions=[{"id": AT_ALL_ID, "name": "全体成员"}],
        )
        self.assertTrue(env["mentions"][0]["is_all"])

    def test_third_party_platform_is_accepted(self):
        env = build_envelope("telegram", chat_id="x", sender_name="y", text="hi")
        self.assertTrue(is_valid(env))

    def test_invalid_platform_raises(self):
        with self.assertRaises(EnvelopeError):
            build_envelope("QQ群", chat_id="x", sender_name="y")

    def test_missing_chat_id_raises(self):
        with self.assertRaises(EnvelopeError):
            build_envelope("qq", sender_name="机器人")

    def test_invalid_dict_validate_returns_errors(self):
        errors = validate({"platform": "telegram", "trace_id": "", "ts": 0})
        self.assertGreaterEqual(len(errors), 3)

    def test_trace_id_unique(self):
        ids = {make_trace_id("qq") for _ in range(500)}
        self.assertEqual(len(ids), 500)

    def test_json_roundtrip_keeps_chinese(self):
        env = build_envelope("wechat", chat_id="g1", sender_name="阿强", text="晚安")
        payload = to_json(env)
        self.assertIn("晚安", payload)
        self.assertFalse(payload.startswith(" "))

    def test_space_event(self):
        env = build_space_event(
            "qq",
            container_id="qzone",
            container_name="QQ空间",
            action="post",
            post_id="p1",
            author_id="10001",
            author_name="小明",
            text="今天天气不错",
            media=[{"kind": "image", "url": "https://x/y.jpg"}],
        )
        self.assertTrue(is_space(env))
        self.assertEqual(env["kind"], "space")
        self.assertEqual(env["chat"]["type"], "channel")
        self.assertEqual(env["payload"]["action"], "post")

    def test_space_event_rejects_bad_action(self):
        with self.assertRaises(EnvelopeError):
            build_space_event("wechat", container_id="moments", action="fly")

    def test_normalize_text_keeps_newlines(self):
        env = build_envelope("qq", chat_id="1", sender_name="a", text="  第一行\n第二行  ")
        self.assertEqual(env["text"], "第一行\n第二行")


class TestDedupe(unittest.TestCase):
    def test_message_id_duplicate(self):
        clock = FakeClock()
        d = Dedupe(clock=clock)
        env = build_envelope("qq", chat_id="1", sender_name="a", text="hi", raw={"message_id": "m1"})
        self.assertFalse(d.seen(env))
        self.assertTrue(d.seen(env))
        self.assertEqual(d.stats()["dup_hits"], 1)

    def test_heuristic_duplicate_in_same_window(self):
        clock = FakeClock()
        d = Dedupe(window=120, clock=clock)
        env = build_envelope("wechat", chat_id="1", sender_name="a", text="在吗", ts=100)
        self.assertFalse(d.seen(env))
        clock.advance(5)
        env2 = build_envelope("wechat", chat_id="1", sender_name="a", text="在吗", ts=105)
        self.assertTrue(d.seen(env2))

    def test_heuristic_different_text_not_duplicate(self):
        clock = FakeClock()
        d = Dedupe(window=120, clock=clock)
        a = build_envelope("wechat", chat_id="1", sender_name="a", text="在吗", ts=100)
        b = build_envelope("wechat", chat_id="1", sender_name="a", text="在的", ts=100)
        self.assertFalse(d.seen(a))
        self.assertFalse(d.seen(b))

    def test_ttl_expires(self):
        clock = FakeClock()
        d = Dedupe(ttl=10, window=120, clock=clock)
        env = build_envelope("wechat", chat_id="1", sender_name="a", text="hi", ts=100)
        self.assertFalse(d.seen(env))
        clock.advance(11)
        self.assertFalse(d.seen(env))

    def test_max_entries_bounded(self):
        d = Dedupe(ttl=600, max_entries=3)
        for i in range(10):
            d.seen_key(f"k{i}")
        self.assertLessEqual(d.stats()["entries"], 3)


class TestRateLimiter(unittest.TestCase):
    def test_burst_then_blocked_then_refill(self):
        clock = FakeClock()
        rl = RateLimiter(rate=1.0, burst=3, clock=clock)
        self.assertTrue(rl.allow("g1"))
        self.assertTrue(rl.allow("g1"))
        self.assertTrue(rl.allow("g1"))
        self.assertFalse(rl.allow("g1"))
        self.assertAlmostEqual(rl.retry_after("g1"), 1.0)
        clock.advance(1.0)
        self.assertTrue(rl.allow("g1"))

    def test_keys_are_isolated(self):
        rl = RateLimiter(rate=1.0, burst=1)
        self.assertTrue(rl.allow("a"))
        self.assertFalse(rl.allow("a"))
        self.assertTrue(rl.allow("b"))


class TestRetry(unittest.TestCase):
    def test_success_after_two_failures(self):
        from infra import call_with_retry

        calls = {"n": 0}
        sleeps = []

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("boom")
            return "ok"

        policy = RetryPolicy(attempts=4, base=0.5, factor=2.0, jitter=0.0)
        result = call_with_retry(fn, policy=policy, sleep=lambda s: sleeps.append(s))
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_exhausted_carries_last_error(self):
        from infra import call_with_retry

        def fn():
            raise RuntimeError("always")

        with self.assertRaises(RetryExhausted) as ctx:
            call_with_retry(fn, policy=RetryPolicy(attempts=2, base=0.1, jitter=0.0), sleep=lambda s: None)
        self.assertEqual(ctx.exception.attempts, 2)
        self.assertIsInstance(ctx.exception.last, RuntimeError)

    def test_should_retry_false_propagates(self):
        from infra import call_with_retry

        def fn():
            raise ValueError("no retry")

        with self.assertRaises(ValueError):
            call_with_retry(fn, should_retry=lambda e: not isinstance(e, ValueError), sleep=lambda s: None)

    def test_dead_letter(self):
        dl = DeadLetter(maxlen=2)
        dl.add({"trace_id": "t1"}, reason="超时", attempts=4)
        dl.add({"trace_id": "t2"}, reason="超时", attempts=4)
        dl.add({"trace_id": "t3"}, reason="超时", attempts=4)
        self.assertEqual(dl.stats()["size"], 2)
        self.assertEqual(dl.stats()["total"], 3)
        drained = dl.drain()
        self.assertEqual([i["envelope"]["trace_id"] for i in drained], ["t2", "t3"])
        self.assertEqual(dl.items(), [])


class TestHttpServer(unittest.TestCase):
    def setUp(self):
        def echo(req):
            return ok({"echo": req.payload, "q": req.query})

        def boom(req):
            raise RuntimeError("故意炸")

        self.handle = serve_in_thread(
            0,
            {"POST /echo": echo, "GET /boom": boom, "GET /ping": lambda req: ok("pong")},
            service="io-test",
        )
        self.base = self.handle.url

    def tearDown(self):
        self.handle.stop()

    def _get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=3) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8")

    def _post(self, path: str, payload: dict, method: str = "POST"):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, resp.headers, resp.read().decode("utf-8")

    def test_health_auto(self):
        status, headers, body = self._get("/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["service"], "io-test")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")

    def test_stats_auto(self):
        self._get("/ping")
        status, _, body = self._get("/stats")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(json.loads(body)["http_requests"], 1)

    def test_post_json_and_query(self):
        status, _, body = self._post("/echo?name=abc", {"a": 1})
        self.assertEqual(status, 200)
        data = json.loads(body)["data"]
        self.assertEqual(data["q"]["name"], "abc")
        self.assertEqual(data["echo"], {"a": 1})

    def test_cors_preflight(self):
        req = urllib.request.Request(self.base + "/echo", method="OPTIONS")
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.status, 204)
            self.assertEqual(resp.headers["Access-Control-Allow-Origin"], "*")
            self.assertIn("POST", resp.headers["Access-Control-Allow-Methods"])

    def test_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_route_exception_is_500(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/boom")
        self.assertEqual(ctx.exception.code, 500)

    def test_bad_json_is_400(self):
        req = urllib.request.Request(
            self.base + "/echo", data=b"{not json", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(ctx.exception.code, 400)


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.reg = CommandRegistry()
        self.report = load_all(self.reg)
        for scope, result in self.report.items():
            self.assertEqual(result["failed"], [], f"{scope} 插件加载失败")

    def context(self, text, *, platform="qq", admin=False, chat_type="group"):
        return CommandContext(
            platform=platform,
            text=text,
            chat_id="1",
            chat_name="测试群",
            sender_id="10001",
            sender_name="小明",
            chat_type=chat_type,
            is_admin=admin,
        )

    def run_cmd(self, text, *, platform="qq", admin=False):
        ctx = self.context(text, platform=platform, admin=admin)
        match = self.reg.resolve(ctx)
        if not match.ok:
            return match, None
        ctx.args = match.args
        ctx.argv = match.argv
        return match, match.command.handler(ctx)

    def test_plugin_directories_loaded(self):
        self.assertEqual(self.reg.stats()["total"], 14)
        self.assertEqual(sorted(self.report), ["common", "qq", "wechat"])

    def test_parse_command(self):
        self.assertEqual(parse_command("/管理"), ("管理", ""))
        self.assertEqual(parse_command("／普通 2"), ("普通", "2"))
        self.assertIsNone(parse_command("普通"))
        self.assertIsNone(parse_command("/"))

    def test_qq_normal_menu_has_no_wechat_commands(self):
        match, result = self.run_cmd("/普通")
        self.assertTrue(match.ok)
        self.assertIn("/普通", result.text)
        self.assertNotIn("/朋友圈", result.text)
        self.assertNotIn("/撤回", result.text)

    def test_qq_admin_menu(self):
        match, result = self.run_cmd("/管理", admin=True)
        self.assertTrue(match.ok)
        self.assertIn("/撤回", result.text)
        self.assertIn("/禁言", result.text)
        self.assertNotIn("/朋友圈", result.text)

    def test_wechat_normal_menu_is_platform_scoped(self):
        match, result = self.run_cmd("/普通", platform="wechat")
        self.assertTrue(match.ok)
        self.assertIn("/拍一拍", result.text)
        self.assertIn("/朋友圈", result.text)
        self.assertIn("/点赞", result.text)
        self.assertNotIn("/禁言", result.text)

    def test_admin_menu_requires_admin(self):
        match, result = self.run_cmd("/管理", admin=False)
        self.assertFalse(match.ok)
        self.assertIn("管理员", match.reason)

    def test_qq_only_command_rejected_on_wechat(self):
        match, result = self.run_cmd("/删说说 abc", platform="wechat", admin=True)
        self.assertFalse(match.ok)
        self.assertIn("qq", match.reason)

    def test_wechat_only_command_rejected_on_qq(self):
        match, result = self.run_cmd("/朋友圈", platform="qq")
        self.assertFalse(match.ok)
        self.assertIn("wechat", match.reason)

    def test_help_alias_points_to_normal_menu(self):
        match, result = self.run_cmd("/帮助")
        self.assertTrue(match.ok)
        self.assertEqual(match.command.name, "普通")

    def test_mute_parses_target_and_seconds(self):
        match, result = self.run_cmd("/禁言 60 @小明", admin=True)
        self.assertTrue(match.ok)
        self.assertEqual(result.action["type"], "mute")
        self.assertEqual(result.action["seconds"], 60)
        self.assertEqual(result.action["target"], "小明")

    def test_mute_without_target_tells_usage(self):
        match, result = self.run_cmd("/禁言 60", admin=True)
        self.assertTrue(match.ok)
        self.assertIn("用法", result.text)

    def test_wechat_pat_action(self):
        match, result = self.run_cmd("/拍一拍 老王", platform="wechat")
        self.assertTrue(match.ok)
        self.assertEqual(result.action, {"type": "pat", "target": "老王"})

    def test_moments_limit_clamped(self):
        match, result = self.run_cmd("/朋友圈 99", platform="wechat")
        self.assertTrue(match.ok)
        self.assertEqual(result.action["limit"], 20)

    def test_same_name_command_resolved_per_platform(self):
        self.assertEqual(self.reg.get("禁言", "qq").scope, "qq")
        self.assertEqual(self.reg.get("禁言", "wechat").scope, "wechat")
        match, result = self.run_cmd("/禁言 30 老王", platform="wechat", admin=True)
        self.assertTrue(match.ok)
        self.assertEqual(result.action["type"], "mute")
        self.assertEqual(result.action["chat_id"], "1")

    def test_qq_space_commands(self):
        match, result = self.run_cmd("/空间 今天真不错", admin=True)
        self.assertTrue(match.ok)
        self.assertEqual(result.action["type"], "space_post")
        self.assertEqual(result.action["text"], "今天真不错")
        match, result = self.run_cmd("/删说说 tid-1", admin=True)
        self.assertTrue(match.ok)
        self.assertEqual(result.action["post_id"], "tid-1")

    def test_wechat_like_action(self):
        match, result = self.run_cmd("/点赞 老王 天气", platform="wechat")
        self.assertTrue(match.ok)
        self.assertEqual(result.action["publisher"], "老王")
        self.assertEqual(result.action["keyword"], "天气")


if __name__ == "__main__":
    unittest.main(verbosity=2)
