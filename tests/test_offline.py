#!/usr/bin/env python3
"""离线单元测试：不起网络，纯本地逻辑校验。

运行：
    python tests/test_offline.py
    python -m unittest discover -s tests
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fastdl


class TestHuman(unittest.TestCase):
    def test_units(self):
        self.assertEqual(fastdl.human(512), "512 B")
        self.assertEqual(fastdl.human(1024), "1.0 KiB")
        self.assertEqual(fastdl.human(1536), "1.5 KiB")
        self.assertEqual(fastdl.human(1024 ** 2), "1.0 MiB")
        self.assertEqual(fastdl.human(1024 ** 3), "1.0 GiB")

    def test_negative_does_not_crash(self):
        # 速度计算初值为 0，不应抛异常
        self.assertIsInstance(fastdl.human(0), str)


class TestPrinter(unittest.TestCase):
    def test_no_tty_fallback(self):
        """非 TTY 环境应正常输出而不抛异常、不无限刷新。"""
        p = fastdl.Printer()
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            p.progress(0, 100, 0.0, force=True)
            p.progress(50, 100, 1024.0, force=True)
            p.done()
        self.assertIn("50.0%", buf.getvalue())


class FakeResponse:
    """模拟 urllib 的响应对象，用于测分片抓取逻辑。"""

    def __init__(self, payload: bytes, status=206, headers=None):
        self._buf = payload
        self._pos = 0
        self.status = status
        self.headers = headers or {}

    def read(self, n=-1):
        if n is None or n < 0:
            n = len(self._buf) - self._pos
        chunk = self._buf[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """按请求的Range 返回对应区间数据。"""

    def __init__(self, data: bytes):
        self.data = data
        self.ranges = []

    def open(self, req, timeout=None):
        rng = req.headers.get("Range") if hasattr(req, "headers") else req.get_header("Range")
        self.ranges.append(rng)
        spec = rng.split("=")[1]
        s, e = (int(x) for x in spec.split("-"))
        return FakeResponse(self.data[s:e + 1])


class TestPartMath(unittest.TestCase):
    def test_n_parts_ceil(self):
        for size, chunk, expect in [
            (100, 30, 4),      # 3.33 -> 4
            (90, 30, 3),       # 整除
            (1, 8, 1),
            (0, 8, 0),
        ]:
            n = (size + chunk - 1) // chunk
            self.assertEqual(n, expect, "size=%d chunk=%d" % (size, chunk))

    def test_last_part_is_short(self):
        """最后一个分片长度应小于 chunk，且总长度精确等于 size。"""
        size, chunk = 100, 30
        n = (size + chunk - 1) // chunk
        lens = []
        for i in range(n):
            start = i * chunk
            end = min(start + chunk, size) - 1
            lens.append(end - start + 1)
        self.assertEqual(sum(lens), size)
        self.assertEqual(lens[-1], 10)  # 100 - 90


class TestDownloaderResume(unittest.TestCase):
    """断点续传的核心正确性：分片拼接结果必须与原文件完全一致。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fastdl_test_")
        self.data = bytes((i * 7 + 13) % 256 for i in range(300000))
        self.size = len(self.data)
        self.out = os.path.join(self.tmp, "out.bin")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk(self, **kw):
        params = dict(
            url="http://fake/x", out=self.out, size=self.size,
            sha256=None, threads=4, chunk=65536, retries=2,
            opener=FakeOpener(self.data), token=None, keep_parts=False,
        )
        params.update(kw)
        return fastdl.Downloader(**params)

    def test_full_download_matches(self):
        dl = self._mk()
        rc = dl.run()
        self.assertEqual(rc, 0)
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_resume_from_partial_parts(self):
        """预置部分分片（含一个残缺分片），应只补缺失部分且结果正确。"""
        dl = self._mk()
        os.makedirs(dl.part_dir, exist_ok=True)
        # 完整分片 0、1
        for i in (0, 1):
            with open(dl.part_path(i), "wb") as f:
                f.write(self.data[i * 65536:(i + 1) * 65536])
        # 残缺分片 2：只有一半
        with open(dl.part_path(2), "wb") as f:
            f.write(self.data[2 * 65536:2 * 65536 + 10000])

        pre = dl.done_bytes()
        self.assertGreater(pre, 0)
        self.assertLess(pre, self.size)

        rc = dl.run()
        self.assertEqual(rc, 0)
        with open(self.out, "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_sha256_pass_and_fail(self):
        import hashlib
        good = hashlib.sha256(self.data).hexdigest()

        dl = self._mk(sha256=good)
        self.assertEqual(dl.run(), 0)

        dl2 = self._mk(sha256="0" * 64)
        dl2.out = os.path.join(self.tmp, "out2.bin")
        self.assertEqual(dl2.run(), 3)   # 校验失败
        # 失败时必须保留分片以便重试
        self.assertTrue(os.path.isdir(dl2.part_dir))

    def test_parts_cleaned_on_success(self):
        dl = self._mk()
        dl.run()
        self.assertFalse(os.path.exists(dl.part_dir))

    def test_keep_parts(self):
        dl = self._mk(keep_parts=True)
        dl.run()
        self.assertTrue(os.path.isdir(dl.part_dir))

    def test_size_mismatch_detected(self):
        """服务端给的数据不足时必须报大小不匹配，而不是静默通过。"""
        dl = self._mk(size=self.size + 5000)
        self.assertEqual(dl.run(), 4)


class TestCli(unittest.TestCase):
    def test_no_args_errors(self):
        with self.assertRaises(SystemExit):
            fastdl.main([])

    def test_gh_release_without_tag_errors(self):
        with self.assertRaises(SystemExit):
            fastdl.main(["--gh-release", "owner/repo"])

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as cm:
            fastdl.main(["--version"])
        self.assertEqual(cm.exception.code, 0)


class TestOpeners(unittest.TestCase):
    """验证代理与 TLS 配置。

    urllib 注入 ProxyHandler({}) 后的实际行为（实测 CPython 3.8/ 3.13 一致）：
      · 原ProxyHandler 实例被**移除**
      · 原位替换成一个 UnknownHandler（它没有 proxies 属性）
    所以"代理已禁用"的判据是：**handlers 里没有 proxies 非空的 ProxyHandler**。

    两个必须避开的坑：
    1. 不要断言 handler 类名——UnknownHandler 与 ProxyHandler 的取舍
       依平台和版本而变，不稳定。
    2. **绝对不要调用 opener.open()**——那会真的发起网络请求并阻塞。
    """

    def _active_proxies(self, opener):
        """返回 opener 中真正生效的代理配置（proxies 非空的 ProxyHandler）。"""
        import urllib.request as _u
        return [h.proxies for h in opener.handlers
                if isinstance(h, _u.ProxyHandler) and h.proxies]

    def test_proxy_bypass_by_default(self):
        """use_proxy=False 必须禁用代理（实测本机代理比直连慢 3 倍）。"""
        bypass = fastdl.build_opener(no_tls_verify=False, use_proxy=False)
        self.assertEqual(
            self._active_proxies(bypass), [],
            "use_proxy=False 时不应存在生效的代理配置",
        )

    def test_use_proxy_keeps_default_behavior(self):
        """use_proxy=True 不应禁用代理，应保持 urllib 默认行为。"""
        import urllib.request as _u
        proxied = fastdl.build_opener(no_tls_verify=False, use_proxy=True)
        default = _u.build_opener()
        self.assertEqual(
            self._active_proxies(proxied),
            self._active_proxies(default),
            "use_proxy=True 应与 urllib 默认代理配置一致",
        )

    def test_tls_verify_flag(self):
        op = fastdl.build_opener(no_tls_verify=True, use_proxy=False)
        self.assertTrue(any("HTTPSHandler" in type(h).__name__ for h in op.handlers))


class TestCredentialSafety(unittest.TestCase):
    """凭据安全回归测试。

    起因（安全审查发现）：`gh_api()` 曾**无条件**设置
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    同时又在请求头里发送 Authorization token。这意味着即便用户从没传过
    --no-tls-verify，只要走 urllib 回退路径携带 token，TLS 校验就被静默关闭，
    中间人可直接窃取 token。

    修复后：API 调用默认正常校验证书，仅在调用方显式要求时才放宽；
    且「关闭校验 + 携带凭据」必须显式告警。
    这几个测试钉住该契约，防止将来被改回去。
    """

    def test_gh_api_defaults_to_verifying_tls(self):
        """gh_api 的 no_tls_verify 必须默认 False（即默认校验证书）。"""
        import inspect
        sig = inspect.signature(fastdl.gh_api)
        self.assertIn("no_tls_verify", sig.parameters,
                      "gh_api 必须暴露 no_tls_verify 参数")
        self.assertIs(
            sig.parameters["no_tls_verify"].default, False,
            "gh_api 的 no_tls_verify 默认必须是 False —— 否则带 token 时"
            "会静默放弃 TLS 校验（历史安全缺陷）",
        )

    def test_resolve_gh_release_defaults_to_verifying_tls(self):
        import inspect
        sig = inspect.signature(fastdl.resolve_gh_release)
        self.assertIs(sig.parameters["no_tls_verify"].default, False)

    def test_build_opener_verifies_by_default(self):
        """默认 opener 不应包含关闭校验的 HTTPSHandler。"""
        for use_proxy in (False, True):
            op = fastdl.build_opener(no_tls_verify=False, use_proxy=use_proxy)
            for h in op.handlers:
                if type(h).__name__ != "HTTPSHandler":
                    continue
                # 未显式要求时，HTTPSHandler 不应带 CERT_NONE 上下文
                ctx = getattr(h, "_context", None)
                if ctx is not None:
                    import ssl as _ssl
                    self.assertNotEqual(ctx.verify_mode, _ssl.CERT_NONE)

    def test_warns_when_insecure_and_token(self):
        """关闭校验 + 有 token → 必须告警。"""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            fired = fastdl.warn_insecure_credential(True, "ghp_secret")
        self.assertTrue(fired)
        out = buf.getvalue()
        self.assertIn("安全警告", out)
        self.assertIn("token", out)

    def test_no_warning_without_token(self):
        """只关闭校验、无凭据 → 不告警（此时无凭据可泄露）。"""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            fired = fastdl.warn_insecure_credential(True, None)
        self.assertFalse(fired)
        self.assertEqual(buf.getvalue(), "")

    def test_no_warning_when_tls_verified(self):
        """正常校验证书 → 即便有 token 也不告警。"""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            fired = fastdl.warn_insecure_credential(False, "ghp_secret")
        self.assertFalse(fired)
        self.assertEqual(buf.getvalue(), "")


class TestCliArgValidation(unittest.TestCase):
    """参数校验回归测试。

    起因：README 曾把 `--gh-release owner/repo v1.0.0` 写成正确用法，
    但 tag 必须是独立参数（--tag），导致该命令必然报错。
    这类"文档写错→ 用户踩坑"的问题，用测试钉住。
    """

    def _err(self, argv):
        """捕获 argparse 的错误退出，返回 stderr 文本。"""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as cm:
                fastdl.main(argv)
        self.assertEqual(cm.exception.code, 2, "参数错误应以退出码 2 结束")
        return buf.getvalue()

    def test_gh_release_requires_tag(self):
        self.assertIn("--tag", self._err(["--gh-release", "owner/repo"]))

    def test_gh_release_with_bare_tag_gives_hint(self):
        """README 里的错误写法应触发明确提示，且提示里含正确写法。"""
        out = self._err(["--gh-release", "owner/repo", "v1.0.0"])
        self.assertIn("--tag", out)
        self.assertIn("正确写法", out)
        self.assertIn("v1.0.0", out)

    def test_tag_without_gh_release_rejected(self):
        self.assertIn("--gh-release",
                      self._err(["https://example.com/x.zip", "--tag", "v1.0.0"]))

    def test_no_input_rejected(self):
        self.assertTrue(self._err([]).strip())

    def test_valid_args_pass_validation(self):
        """参数齐全时不应被参数校验拦下。

        只验证「不产生 SystemExit(2)」——后续远端调用是否成功
        取决于 gh 是否可用，不属于离线测试范畴。
        """
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            try:
                fastdl.main(["--gh-release", "owner/repo", "--tag", "v1.0.0"])
            except SystemExit as e:
                self.fail("合法参数不应触发参数校验错误(exit 2)，实际 exit=%s，stderr=%s"
                          % (e.code, buf.getvalue()))
            except RuntimeError:
                pass  # 预期：进入远端调用阶段后失败，与参数校验无关


if __name__ == "__main__":
    unittest.main(verbosity=2)
