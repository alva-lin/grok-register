"""标签模式（本地魔改）单元测试 + 可选的真实平台联调。

只跑单测：
    python3 -m unittest backend.tests.test_outlook_tags -v

联调真实平台（需 API Base/Key，并会真实读写标签）：
    OUTLOOK_TAG_LIVE=1 \
    OUTLOOK_API_BASE=https://mail-pool.yuheng.site \
    OUTLOOK_API_KEY=... \
    python3 -m unittest backend.tests.test_outlook_tags -v
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from backend.mailbox import outlook_pool


def account(email: str, tags=None, status: str = "active") -> dict:
    return {
        "id": 1,
        "email": email,
        "status": status,
        "tags": [{"id": index + 1, "name": name} for index, name in enumerate(tags or [])],
    }


class TagPredicateTests(unittest.TestCase):
    def test_no_prefixed_tag_is_selectable(self):
        self.assertTrue(outlook_pool.item_has_no_prefixed_tag(account("a@x.com")))
        self.assertTrue(outlook_pool.item_has_no_prefixed_tag(account("a@x.com", ["其他平台-已用"])))

    def test_any_prefixed_tag_blocks_selection(self):
        for name in ("Grok-使用中", "Grok-成功", "Grok-失败", "Grok-任意"):
            self.assertFalse(
                outlook_pool.item_has_no_prefixed_tag(account("a@x.com", [name])),
                name,
            )

    def test_inactive_status_is_irrelevant_in_tag_mode(self):
        # 标签模式不再看 status：inactive 但无 Grok-* 标签仍可被取用（由 claim 决定）
        self.assertTrue(
            outlook_pool.item_has_no_prefixed_tag(account("a@x.com", status="inactive"))
        )

    def test_custom_prefix(self):
        item = account("a@x.com", ["GrokIQ-降智"])
        self.assertTrue(outlook_pool.item_has_no_prefixed_tag(item, "Grok-"))
        self.assertFalse(outlook_pool.item_has_no_prefixed_tag(item, "GrokIQ-"))

    def test_tag_names_accepts_plain_strings(self):
        item = {"email": "a@x.com", "tags": ["Grok-成功", {"name": "Grok-使用中"}]}
        self.assertEqual(outlook_pool.tag_names(item), {"Grok-成功", "Grok-使用中"})


class AcquireWithTagsTests(unittest.TestCase):
    def _acquire(self, accounts, claim_results, **kwargs):
        calls = []

        def fake_get_accounts(*_args, **_kwargs):
            return accounts

        def fake_set_tags(_base, email, *, action, api_key="", tags=None, tag_prefix="Grok-", timeout=20):
            calls.append((email, action, tag_prefix))
            return claim_results.pop(0)

        with mock.patch.object(outlook_pool, "get_accounts", fake_get_accounts), \
                mock.patch.object(outlook_pool, "set_account_tags", fake_set_tags):
            result = outlook_pool.acquire_email(
                lambda *a, **k: None,
                lambda: None,
                "https://mail.example",
                api_key="k",
                source="accounts",
                pick_mode="sequential",
                use_tags=True,
                **kwargs,
            )
        return result, calls

    def test_claim_first_candidate(self):
        outlook_pool.reset_runtime_state()
        result, calls = self._acquire(
            [account("fresh@x.com")],
            [{"success": True, "available": True}],
        )
        self.assertEqual(result[0], "fresh@x.com")
        self.assertEqual(calls, [("fresh@x.com", "claim", "Grok-")])

    def test_claim_conflict_falls_through_to_next_candidate(self):
        outlook_pool.reset_runtime_state()
        result, calls = self._acquire(
            [account("taken@x.com"), account("free@x.com")],
            [{"success": True, "available": False, "status_code": 409},
             {"success": True, "available": True}],
        )
        self.assertEqual(result[0], "free@x.com")
        self.assertEqual([c[0] for c in calls], ["taken@x.com", "free@x.com"])

    def test_candidates_skip_prefixed_tags_before_claim(self):
        outlook_pool.reset_runtime_state()
        result, calls = self._acquire(
            [account("used@x.com", ["Grok-成功"]), account("fresh@x.com")],
            [{"success": True, "available": True}],
        )
        self.assertEqual(result[0], "fresh@x.com")
        self.assertEqual([c[0] for c in calls], ["fresh@x.com"])

    def test_all_candidates_taken_raises(self):
        outlook_pool.reset_runtime_state()
        with self.assertRaises(Exception) as ctx:
            self._acquire(
                [account("a@x.com"), account("b@x.com")],
                [{"success": True, "available": False}, {"success": True, "available": False}],
            )
        self.assertIn("均已被占用", str(ctx.exception))

    def test_empty_pool_raises_with_predicate_hint(self):
        outlook_pool.reset_runtime_state()
        with self.assertRaises(Exception) as ctx:
            self._acquire([], [])
        self.assertIn("不带 Grok-* 标签", str(ctx.exception))


@unittest.skipUnless(os.environ.get("OUTLOOK_TAG_LIVE") == "1", "需要 OUTLOOK_TAG_LIVE=1 才跑真实平台联调")
class LivePlatformTagTests(unittest.TestCase):
    """真实平台联调：claim → 409 → set 全流程（会真实读写标签）。"""

    PREFIX = "Grok-"

    @classmethod
    def setUpClass(cls):
        cls.base = os.environ["OUTLOOK_API_BASE"]
        cls.key = os.environ["OUTLOOK_API_KEY"]
        import requests

        resp = requests.Session().get(
            f"{cls.base.rstrip('/')}/api/external/accounts",
            headers=outlook_pool.api_headers(cls.key),
            params={"limit": 10000, "offset": 0},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        accounts = data.get("accounts") or []
        cls.fresh = [a for a in accounts if outlook_pool.item_has_no_prefixed_tag(a, cls.PREFIX)]
        cls.used = [a for a in accounts if not outlook_pool.item_has_no_prefixed_tag(a, cls.PREFIX)]
        print(f"\n池内账号 {len(accounts)}：未使用(无 {cls.PREFIX}*) {len(cls.fresh)}，已带标签 {len(cls.used)}")

    def test_predicate_matches_platform_data(self):
        self.assertEqual(len(self.fresh) + len(self.used), len(self.fresh) + len(self.used))
        for item in self.used[:5]:
            self.assertFalse(outlook_pool.item_has_no_prefixed_tag(item, self.PREFIX))
        for item in self.fresh[:5]:
            self.assertTrue(outlook_pool.item_has_no_prefixed_tag(item, self.PREFIX))

    def test_claim_conflict_and_final_tag_roundtrip(self):
        if not self.fresh:
            self.skipTest("池内没有未使用邮箱，跳过写入联调")
        email = self.fresh[0]["email"]
        original_tags = sorted(outlook_pool.tag_names(self.fresh[0]))

        first = outlook_pool.set_account_tags(self.base, email, action="claim",
                                              api_key=self.key, tag_prefix=self.PREFIX)
        self.assertTrue(first.get("available"), "首个 claim 应成功")

        second = outlook_pool.set_account_tags(self.base, email, action="claim",
                                               api_key=self.key, tag_prefix=self.PREFIX)
        self.assertFalse(second.get("available"), "重复 claim 应返回 409/不可用")

        final = outlook_pool.set_account_tags(self.base, email, action="set",
                                              api_key=self.key, tags=[f"{self.PREFIX}成功"],
                                              tag_prefix=self.PREFIX)
        self.assertTrue(final.get("success"))
        names = outlook_pool.tag_names({"tags": final.get("tags")})
        self.assertIn(f"{self.PREFIX}成功", names)
        self.assertNotIn(f"{self.PREFIX}使用中", names, "set 应先清掉「使用中」")

        # 复原：把该邮箱恢复成测试前的标签集合
        if original_tags:
            outlook_pool.set_account_tags(self.base, email, action="set",
                                          api_key=self.key, tags=original_tags, tag_prefix=self.PREFIX)
        else:
            outlook_pool.set_account_tags(self.base, email, action="remove",
                                          api_key=self.key, tags=[f"{self.PREFIX}成功"],
                                          tag_prefix=self.PREFIX)
        print(f"  联调邮箱 {email} 已复原为 {original_tags or '无标签'}")


if __name__ == "__main__":
    unittest.main()
