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
    def test_proxy_bypass_by_default(self):
        """默认 opener 必须绕过系统代理（实测代理常比直连慢）。

        urllib 的细节：注入 ProxyHandler({}) 会把默认的 ProxyHandler
        顶替成一个 UnknownHandler。所以判据是——
          use_proxy=False → 有 UnknownHandler、无 ProxyHandler（禁用代理）
          use_proxy=True  → 保留原生 ProxyHandler（读环境变量）
        """
        bypass_names = [type(h).__name__
                        for h in fastdl.build_opener(False, False).handlers]
        proxied_names = [type(h).__name__
                         for h in fastdl.build_opener(False, True).handlers]

        self.assertIn("UnknownHandler", bypass_names,
                      "use_proxy=False 应注入 ProxyHandler({})")
        self.assertNotIn("ProxyHandler", bypass_names,
                         "use_proxy=False 不应保留读环境变量的 ProxyHandler")
        self.assertIn("ProxyHandler", proxied_names,
                      "use_proxy=True 应保留原生 ProxyHandler")

    def test_tls_verify_flag(self):
        op = fastdl.build_opener(no_tls_verify=True, use_proxy=False)
        self.assertTrue(any("HTTPSHandler" in type(h).__name__ for h in op.handlers))


if __name__ == "__main__":
    unittest.main(verbosity=2)
