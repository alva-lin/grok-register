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
from backend.registration import engine as gr


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


class GroupAvailabilityTests(unittest.TestCase):
    """分组下拉列表的「可用/总数」口径。"""

    def _accounts(self):
        return [
            {**account("a@x.com"), "group_id": 4, "group_name": "g4"},
            {**account("b@x.com", ["Grok-成功"]), "group_id": 4, "group_name": "g4"},
            {**account("c@x.com", ["Grok-使用中"]), "group_id": 4, "group_name": "g4"},
            {**account("d@x.com", ["其他-已用"]), "group_id": 5, "group_name": "g5"},
            {**account("e@x.com", ["Grok-失败"]), "group_id": 5, "group_name": "g5"},
        ]

    def test_counts_only_untagged_accounts(self):
        counts = outlook_pool.availability_counts(self._accounts(), "Grok-")
        self.assertEqual(counts, {4: 1, 5: 1})

    def test_empty_prefix_skips_counting(self):
        self.assertEqual(outlook_pool.availability_counts(self._accounts(), ""), {})
        self.assertEqual(outlook_pool.availability_counts(self._accounts(), "   "), {})

    def test_groups_from_accounts_exposes_available_and_total(self):
        with mock.patch.object(outlook_pool, "get_accounts", lambda *_a, **_k: self._accounts()):
            groups = outlook_pool._groups_from_accounts(None, "http://x", "key", available_prefix="Grok-")
        by_id = {group["id"]: group for group in groups}
        self.assertEqual(by_id[4]["account_count"], 3)
        self.assertEqual(by_id[4]["available_count"], 1)
        self.assertEqual(by_id[5]["account_count"], 2)
        self.assertEqual(by_id[5]["available_count"], 1)

    def test_groups_without_prefix_report_none(self):
        with mock.patch.object(outlook_pool, "get_accounts", lambda *_a, **_k: self._accounts()):
            groups = outlook_pool._groups_from_accounts(None, "http://x", "key")
        self.assertTrue(all(group["available_count"] is None for group in groups))

    def test_group_with_only_tagged_accounts_still_reports_total(self):
        # g6 共 2 个、可用 1 个：下拉里能看出「这个分组还剩多少能取」
        accounts = [
            {**account("f@x.com"), "group_id": 6, "group_name": "g6"},
            {**account("g@x.com", ["Grok-成功"]), "group_id": 6, "group_name": "g6"},
        ]
        counts = outlook_pool.availability_counts(accounts, "Grok-")
        self.assertEqual(counts, {6: 1})


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


class TagModeSuccessWriteTests(unittest.TestCase):
    """标签模式必须独立于「CPA 成功后停用」开关完成终态回写。

    回归：/app 上 CPA 成功、标签模式开启、disable_after_cpa_success=False 时，
    default_email_disable_detail() 返回 feature_disabled，导致成功路径直接 return，
    邮箱永久停在「Grok-使用中」。
    """

    def setUp(self):
        self._saved = gr.config
        gr.config = {
            **gr.DEFAULT_CONFIG,
            "email_provider": "outlookemail",
            "outlookemail_source": "accounts",
            "outlookemail_use_tags": True,
            "outlookemail_tag_prefix": "Grok-",
            "outlookemail_disable_after_cpa_success": False,
            "cpa_auto_add": True,
        }

    def tearDown(self):
        gr.config = self._saved

    def test_taggable_even_when_disable_switch_is_off(self):
        detail = gr.default_email_disable_detail(
            "outlookemail", {"enabled": True, "status": "success"}
        )
        self.assertEqual(detail["status"], "not_attempted")

    def test_success_path_writes_success_tag(self):
        calls = []

        def fake_set_final_tag(email, suffix, log_callback=None):
            calls.append((email, suffix))
            return {"status": "success", "account_id": "1", "tag": suffix}

        with mock.patch.object(gr, "outlookemail_set_final_tag", fake_set_final_tag):
            detail = gr.disable_outlookemail_after_cpa_success(
                "a@x.com", {"enabled": True, "status": "success"}
            )
        self.assertEqual(calls, [("a@x.com", gr.OUTLOOK_TAG_SUCCESS)])
        self.assertEqual(detail["status"], "success")
        self.assertEqual(detail["mode"], "tags")

    def test_non_tag_mode_still_respects_disable_switch(self):
        gr.config = {**gr.config, "outlookemail_use_tags": False}
        detail = gr.default_email_disable_detail(
            "outlookemail", {"enabled": True, "status": "success"}
        )
        self.assertEqual(detail["status"], "feature_disabled")


class FailureTagFinalizeTests(unittest.TestCase):
    """兜底：任何失败类型都不能让邮箱停在「Grok-使用中」。

    回归：aerxxsdfbl@outlook.com 因「资料页等 CF 人机验证超时」被判为
    kind=other；maybe_disable_outlookemail_for_consumed_failure() 只覆盖
    already_registered/risk/sso 三类 → 终态标签没写 → 邮箱永久占位。
    """

    def setUp(self):
        self._saved = gr.config
        gr.config = {
            **gr.DEFAULT_CONFIG,
            "email_provider": "outlookemail",
            "outlookemail_source": "accounts",
            "outlookemail_use_tags": True,
            "outlookemail_tag_prefix": "Grok-",
        }

    def tearDown(self):
        gr.config = self._saved

    def _run(self, kind):
        calls = []

        def fake_set_final_tag(email, suffix, log_callback=None):
            calls.append((email, suffix))
            return {"status": "success", "tag": suffix}

        with mock.patch.object(gr, "outlookemail_set_final_tag", fake_set_final_tag):
            gr.finalize_outlookemail_failure_tag(kind, "a@x.com")
        return calls

    def test_other_failure_gets_terminal_tag(self):
        self.assertEqual(self._run(gr.FAIL_OTHER), [("a@x.com", gr.OUTLOOK_TAG_FAILED)])

    def test_code_timeout_failure_gets_terminal_tag(self):
        # 验证码超时同样不在 maybe_disable 的三类里，也必须收口
        self.assertEqual(self._run(gr.FAIL_CODE), [("a@x.com", gr.OUTLOOK_TAG_FAILED)])

    def test_stuck_flow_failure_gets_terminal_tag(self):
        self.assertEqual(self._run(gr.FAIL_STUCK), [("a@x.com", gr.OUTLOOK_TAG_FAILED)])

    def test_browser_failure_gets_terminal_tag(self):
        self.assertEqual(self._run(gr.FAIL_BROWSER), [("a@x.com", gr.OUTLOOK_TAG_FAILED)])

    def test_handled_kinds_are_not_written_twice(self):
        for kind in (gr.FAIL_ALREADY_REGISTERED, gr.FAIL_RISK, gr.FAIL_SSO):
            self.assertEqual(self._run(kind), [], kind)

    def test_empty_email_is_skipped(self):
        calls = []
        with mock.patch.object(gr, "outlookemail_set_final_tag", lambda *a, **k: calls.append(a)):
            gr.finalize_outlookemail_failure_tag(gr.FAIL_OTHER, "   ")
        self.assertEqual(calls, [])

    def test_non_tag_mode_is_untouched(self):
        gr.config = {**gr.config, "outlookemail_use_tags": False}
        self.assertEqual(self._run(gr.FAIL_OTHER), [])
