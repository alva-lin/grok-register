"""注册代理身份轮换（本地魔改）单元测试。

只覆盖纯函数与身份分配逻辑，不启动浏览器。
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from backend.registration import engine as gr


class IdentityPoolTests(unittest.TestCase):
    def tearDown(self):
        gr.config = dict(gr.DEFAULT_CONFIG)
        gr._rotation_counter.clear()

    def test_auto_generates_base_sequence(self):
        gr.config = {**gr.DEFAULT_CONFIG, "register_proxy_identity_base": "grok-reg"}
        pool = gr.proxy_identity_pool()
        self.assertEqual(len(pool), 500)
        self.assertEqual(pool[0], "grok-reg-1")
        self.assertEqual(pool[-1], "grok-reg-500")

    def test_explicit_pool_wins_and_is_split_on_any_separator(self):
        gr.config = {
            **gr.DEFAULT_CONFIG,
            "register_proxy_identity_pool": "a-1, a-2\n  a-3;  a-4 a-5",
        }
        self.assertEqual(gr.proxy_identity_pool(), ["a-1", "a-2", "a-3", "a-4", "a-5"])

    def test_identity_characters_are_sanitized(self):
        gr.config = {**gr.DEFAULT_CONFIG, "register_proxy_identity_pool": "ok-1, bad:id@x, 好"}
        # 非法字符被剔除；全是非法字符的条目被丢弃
        self.assertEqual(gr.proxy_identity_pool(), ["ok-1", "badidx"])

    def test_unsafe_base_falls_back(self):
        gr.config = {**gr.DEFAULT_CONFIG, "register_proxy_identity_base": "!!!###"}
        self.assertEqual(gr.proxy_identity_pool()[0], "grok-reg-1")


class RotationEnabledTests(unittest.TestCase):
    def tearDown(self):
        gr.config = dict(gr.DEFAULT_CONFIG)

    def test_requires_both_switch_and_placeholder(self):
        template = "http://1024Proxy-5m.{account}:tok@host:50001"
        gr.config = {**gr.DEFAULT_CONFIG, "proxy": template, "register_proxy_identity_rotation": False}
        self.assertFalse(gr.proxy_identity_rotation_enabled())
        gr.config = {**gr.DEFAULT_CONFIG, "proxy": template, "register_proxy_identity_rotation": True}
        self.assertTrue(gr.proxy_identity_rotation_enabled())

    def test_placeholder_missing_disables_rotation(self):
        gr.config = {
            **gr.DEFAULT_CONFIG,
            "proxy": "http://1024Proxy-5m.grok-register:tok@host:50001",
            "register_proxy_identity_rotation": True,
        }
        self.assertFalse(gr.proxy_identity_rotation_enabled())


class GetProxiesTests(unittest.TestCase):
    def tearDown(self):
        gr.config = dict(gr.DEFAULT_CONFIG)
        gr._rotation_counter.clear()
        gr._rotation_thread_state = threading.local()

    def test_fixed_proxy_is_returned_untouched(self):
        proxy = "http://1024Proxy-5m.grok-register:tok@100.64.20.6:50001"
        gr.config = {**gr.DEFAULT_CONFIG, "proxy": proxy}
        self.assertEqual(gr.get_proxies(), {"http": proxy, "https": proxy})

    def test_empty_proxy_returns_empty(self):
        gr.config = {**gr.DEFAULT_CONFIG, "proxy": ""}
        self.assertEqual(gr.get_proxies(), {})

    def test_same_thread_reuses_one_identity(self):
        gr.config = {
            **gr.DEFAULT_CONFIG,
            "proxy": "http://1024Proxy-5m.{account}:tok@100.64.20.6:50001",
            "register_proxy_identity_rotation": True,
        }
        first = gr.get_proxies()["https"]
        second = gr.get_proxies()["https"]
        self.assertEqual(first, second)
        self.assertIn("1024Proxy-5m.grok-reg-1:", first)

    def test_each_thread_gets_its_own_identity(self):
        gr.config = {
            **gr.DEFAULT_CONFIG,
            "proxy": "http://1024Proxy-5m.{account}:tok@100.64.20.6:50001",
            "register_proxy_identity_rotation": True,
        }
        results: dict[str, str] = {}
        barrier = threading.Barrier(3)

        def worker(name: str) -> None:
            barrier.wait()
            results[name] = gr.get_proxies()["https"]

        threads = [threading.Thread(target=worker, args=(f"t{index}",)) for index in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        identities = sorted(value.split("//")[1].split(":")[0] for value in results.values())
        self.assertEqual(identities, ["1024Proxy-5m.grok-reg-1", "1024Proxy-5m.grok-reg-2", "1024Proxy-5m.grok-reg-3"])

    def test_pool_wraps_around(self):
        gr.config = {
            **gr.DEFAULT_CONFIG,
            "proxy": "http://1024Proxy-5m.{account}:tok@host:50001",
            "register_proxy_identity_rotation": True,
            "register_proxy_identity_pool": "only-1, only-2",
        }
        seen = []
        for _ in range(5):
            gr._rotation_thread_state = threading.local()  # 模拟新任务线程
            seen.append(gr.get_proxies()["https"].split("//")[1].split(":")[0])
        self.assertEqual(
            seen,
            ["1024Proxy-5m.only-1", "1024Proxy-5m.only-2", "1024Proxy-5m.only-1", "1024Proxy-5m.only-2", "1024Proxy-5m.only-1"],
        )

    def test_identity_is_not_reused_across_templates(self):
        gr.config = {
            **gr.DEFAULT_CONFIG,
            "proxy": "http://1024Proxy-5m.{account}:tok@host:50001",
            "register_proxy_identity_rotation": True,
        }
        gr.get_proxies()
        # template 变了就不该复用旧缓存；每个 template 各自维护计数器
        gr.config["proxy"] = "http://other.{account}:tok@host:50001"
        self.assertIn("other.grok-reg-1", gr.get_proxies()["https"])


if __name__ == "__main__":
    unittest.main()
