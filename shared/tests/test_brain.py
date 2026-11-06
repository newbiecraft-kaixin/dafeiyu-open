"""brain 自检：身份解析、人设拼装、profile 渲染、NDJSON 解析、会话存储。

不真实调用模型；DSH 的真实对话由 service 启动后的自检覆盖。

运行：python -m unittest discover -s tests -v   （工作目录 <仓库根>/shared）
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1]
PROJECT = Path(__file__).resolve().parents[2]
BRAIN = PROJECT / "services" / "brain"

sys.path.insert(0, str(SHARED))
sys.path.insert(0, str(BRAIN))

import roles as roles_mod  # noqa: E402
from harness import (  # noqa: E402
    DISABLED_TOOL_IDS,
    Harness,
    HarnessError,
    SessionStore,
    build_key,
    load_persona,
    parse_stream,
    render_patch,
    strip_markdown_header,
)


PERSONA = """# 测试人格
> 说明头

---

你是测试角色，只回复正文。
"""


def _config(tmp: Path, *, identities=None, admins=None, roles_extra=None) -> roles_mod.RolesConfig:
    persona = tmp / "persona.md"
    persona.write_text(PERSONA, encoding="utf-8")
    data = {
        "default_role": "甲",
        "platform_defaults": {"qq": "甲", "wechat": "乙"},
        "admins": admins if admins is not None else {"qq": ["1001"]},
        "identities": identities
        if identities is not None
        else [
            {"platform": "qq", "user_id": "1001", "role": "甲", "note": "主人"},
            {"platform": "*", "name": "阿乙", "role": "乙"},
        ],
        "harness": {
            "dsh_root": str(tmp / "dsh"),
            "dsh_home": str(tmp / "dsh-home"),
            "timeout_s": 5,
            "max_parallel": 1,
            "dotenv": [],
        },
        "roles": {
            "甲": {
                "display": "甲",
                "profile": "brain-a",
                "credential": {"mode": "account"},
                "model": "deepseek-flash",
                "reasoning_effort": "high",
                "persona_files": ["persona.md"],
            },
            "乙": {
                "display": "乙",
                "profile": "brain-b",
                "credential": {"mode": "api_key", "env": "TEST_ROLE_KEY"},
                "model": "deepseek-flash",
                "reasoning_effort": "medium",
                "persona_files": ["persona.md"],
            },
        },
    }
    if roles_extra:
        data["roles"].update(roles_extra)
    return roles_mod.RolesConfig.from_dict(data, project_root=tmp)


class TestRoles(unittest.TestCase):
    def test_resolve_user_id_and_admin(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = _config(Path(raw))
            route = cfg.resolve(platform="qq", user_id="1001", name="路人")
            self.assertEqual(route.role.name, "甲")
            self.assertTrue(route.is_admin)
            self.assertIn("identity", route.reason)

    def test_resolve_name_and_platform_default(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = _config(Path(raw))
            route = cfg.resolve(platform="qq", user_id="999", name="阿乙")
            self.assertEqual(route.role.name, "乙")
            self.assertFalse(route.is_admin)

            route = cfg.resolve(platform="wechat", user_id="999", name="路人")
            self.assertEqual(route.role.name, "乙")
            self.assertIn("platform_default", route.reason)

            route = cfg.resolve(platform="telegram", user_id="999")
            self.assertEqual(route.role.name, "甲")
            self.assertEqual(route.reason, "default_role")

    def test_chat_scoped_identity_wins(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = _config(
                Path(raw),
                identities=[
                    {"platform": "qq", "user_id": "1001", "role": "甲"},
                    {"platform": "qq", "chat_id": "g1", "name": "阿乙", "role": "乙"},
                ],
            )
            self.assertEqual(cfg.resolve(platform="qq", user_id="1001").role.name, "甲")
            self.assertEqual(
                cfg.resolve(platform="qq", user_id="x", name="阿乙", chat_id="g1").role.name, "乙"
            )
            self.assertEqual(
                cfg.resolve(platform="qq", user_id="x", name="阿乙", chat_id="g2").role.name, "甲"
            )

    def test_invalid_configs(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            persona = tmp / "p.md"
            persona.write_text("hi", encoding="utf-8")
            bad = {
                "roles": {"甲": {"credential": {"mode": "api_key"}, "profile": "x"}},
            }
            with self.assertRaises(roles_mod.RolesError):
                roles_mod.RolesConfig.from_dict(bad, project_root=tmp)

            bad2 = {"default_role": "不存在", "roles": {"甲": {"profile": "x"}}}
            with self.assertRaises(roles_mod.RolesError):
                roles_mod.RolesConfig.from_dict(bad2, project_root=tmp)

            bad3 = {
                "roles": {"甲": {"profile": "x"}},
                "identities": [{"platform": "qq", "user_id": "1", "role": "不存在"}],
            }
            with self.assertRaises(roles_mod.RolesError):
                roles_mod.RolesConfig.from_dict(bad3, project_root=tmp)

    def test_public_summary_has_no_secret(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = _config(Path(raw))
            data = json.dumps(cfg.summary(), ensure_ascii=False)
            self.assertIn("TEST_ROLE_KEY", data)
            self.assertNotIn("sk-", data)


class TestPersona(unittest.TestCase):
    def test_strip_header(self):
        self.assertEqual(strip_markdown_header(PERSONA), "你是测试角色，只回复正文。")
        self.assertEqual(strip_markdown_header("没有分隔线\n正文"), "没有分隔线\n正文")

    def test_load_persona_merges_files(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            a = tmp / "a.md"
            b = tmp / "b.md"
            a.write_text(PERSONA, encoding="utf-8")
            b.write_text("【补充】\n- 更黏人一点。\n", encoding="utf-8")
            text = load_persona([a, b])
            self.assertIn("你是测试角色", text)
            self.assertIn("更黏人", text)
            self.assertNotIn("测试人格", text)

    def test_missing_persona(self):
        with self.assertRaises(HarnessError):
            load_persona([Path("C:/不存在的人设.md")])


class TestProfile(unittest.TestCase):
    def test_render_patch_account(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = _config(Path(raw))
            role = cfg.role("甲")
            patch = render_patch(role, "你是甲。")
            self.assertIn("provider: deepseek-account", patch)
            self.assertNotIn("deepseek-official", patch)
            self.assertNotIn("apiKeyEnv", patch)
            self.assertIn("    personaPrefix: |-", patch)
            self.assertIn("      你是甲。", patch)
            for tool_id in DISABLED_TOOL_IDS:
                self.assertIn(f"- id: {tool_id}", patch)
            self.assertIn("- id: tool-fs\n  disabled: true", patch)

    def test_render_patch_api_key(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = _config(Path(raw))
            role = cfg.role("乙")
            patch = render_patch(role, "你是乙。")
            self.assertIn("provider: deepseek-official", patch)
            self.assertIn("apiKeyEnv: TEST_ROLE_KEY", patch)
            self.assertNotIn("- id: deepseek-account", patch)

    def test_ensure_profile_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = _config(Path(raw))
            harness = Harness(cfg)
            for role in cfg.roles.values():
                directory = harness.ensure_profile(role)
                self.assertTrue((directory / "package.json").is_file())
                self.assertTrue((directory / "cordis.yml").is_file())
                self.assertTrue((directory / "cordis.patch.yml").is_file())
                self.assertTrue(role.workspace.is_dir())
                package = (directory / "package.json").read_text(encoding="utf-8")
                self.assertFalse(package.startswith("\ufeff"))
                json.loads(package)
                self.assertIn('"@deepseek-ai/dsh-base"', package)
                self.assertIn('"@deepseek-ai/dsh-headless"', package)
                self.assertIn(role.profile, package)
            # 再跑一次内容不变，说明是幂等生成
            harness._profiles.clear()
            for role in cfg.roles.values():
                directory = harness.ensure_profile(role)
                self.assertEqual(
                    (directory / "cordis.patch.yml").read_text(encoding="utf-8"),
                    render_patch(role, load_persona(role.persona_files)),
                )


class TestStream(unittest.TestCase):
    def test_final_wins(self):
        lines = [
            '{"type":"session","sessionId":"s-1"}',
            '{"type":"status","phase":"turn_start","turn":1}',
            '{"type":"text","text":"第一段"}',
            '{"type":"final","text":"完整回复"}',
        ]
        parsed = parse_stream(lines)
        self.assertEqual(parsed.session_id, "s-1")
        self.assertEqual(parsed.reply, "完整回复")
        self.assertTrue(parsed.saw_turn)

    def test_text_fallback_and_error(self):
        lines = ['{"type":"text","text":"只有文本"}', '{"type":"error","message":"鉴权失败"}']
        parsed = parse_stream(lines)
        self.assertEqual(parsed.reply, "只有文本")
        self.assertEqual(parsed.error, "鉴权失败")
        self.assertFalse(parsed.saw_turn)

    def test_bad_lines_ignored(self):
        parsed = parse_stream(["[io-wechat]", "not json", "", "{bad}"])
        self.assertEqual(parsed.reply, "")
        self.assertEqual(parsed.session_id, "")


class TestSessions(unittest.TestCase):
    def test_roundtrip_and_drop(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "sessions.json"
            store = SessionStore(path)
            self.assertEqual(store.get("k"), "")
            store.set("k", "s-1")
            self.assertEqual(store.get("k"), "s-1")
            self.assertEqual(SessionStore(path).get("k"), "s-1")
            store.drop("k")
            self.assertEqual(store.get("k"), "")
            self.assertEqual(SessionStore(path).get("k"), "")

    def test_corrupt_file_tolerated(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "sessions.json"
            path.write_text("{not json", encoding="utf-8")
            store = SessionStore(path)
            store.set("k", "s-2")
            self.assertEqual(SessionStore(path).get("k"), "s-2")

    def test_build_key(self):
        with tempfile.TemporaryDirectory() as raw:
            cfg = _config(Path(raw))
            role = cfg.role("甲")
            self.assertEqual(build_key(role, "qq", "g1", "group", "1001"), "甲|qq|group|g1")
            self.assertEqual(build_key(role, "qq", "g1", "private", "1001"), "甲|qq|private|1001")


if __name__ == "__main__":
    unittest.main()
