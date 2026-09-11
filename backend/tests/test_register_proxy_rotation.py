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


class TaskBoundaryIdentityTests(unittest.TestCase):
    """任务边界必须换身份。

    回归：50 个注册任务 / 4 个 worker 实测只有 10 个不同出口 IP、单 IP 最多 13 次。
    根因是身份按**线程**缓存且从不清除，worker 连续跑任务时一直复用同一条身份。
    """

    def setUp(self):
        self._saved = gr.config
        gr.config = {
            **gr.DEFAULT_CONFIG,
            "proxy": "http://register.{account}:tok@100.64.20.6:50001",
            "register_proxy_identity_rotation": True,
        }
        gr._rotation_counter.clear()

    def tearDown(self):
        gr.config = self._saved
        gr._rotation_thread_state = threading.local()
        gr._rotation_counter.clear()

    def _identity(self) -> str:
        return gr.get_proxies()["https"].split("//")[1].split(":")[0]

    def test_same_task_keeps_one_identity(self):
        first = self._identity()
        for _ in range(5):
            self.assertEqual(self._identity(), first)

    def test_each_task_gets_a_new_identity(self):
        """模拟一个 worker 线程连跑 5 个任务：应当拿到 5 条不同身份。"""
        seen = []
        for _ in range(5):
            gr.begin_proxy_identity_task()
            seen.append(self._identity())
        self.assertEqual(
            seen,
            [
                "register.grok-reg-1",
                "register.grok-reg-2",
                "register.grok-reg-3",
                "register.grok-reg-4",
                "register.grok-reg-5",
            ],
        )

    def test_worker_reuse_without_boundary_is_the_bug(self):
        """不清缓存时一直复用同一条（证明清缓存是必要的）。"""
        first = self._identity()
        self.assertEqual(self._identity(), first)
        self.assertEqual(self._identity(), first)

    def test_boundary_is_safe_when_rotation_disabled(self):
        gr.config = {**gr.config, "register_proxy_identity_rotation": False}
        gr.begin_proxy_identity_task()  # 不应抛异常
        # 轮换关闭时占位符原样保留（不替换）
        self.assertEqual(self._identity(), "register.{account}")
