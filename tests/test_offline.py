#!/usr/bin/env python3
"""离线单元测试：不起网络，纯本地逻辑校验。

运行（两种放法都支持）：
    # 放在仓库里（tests/ 子目录）
    python tests/test_offline.py

    # 从 release 附件平铺下载到与 fastdl.py 同级目录
    python test_offline.py

    # 或用 unittest 发现
    python -m unittest discover -s tests
"""
import os
import shutil
import sys
import tempfile
import unittest

# 兼容两种放置方式：
#   · tests/test_offline.py  → fastdl.py 在上一级
#   · test_offline.py        → fastdl.py 在同级
#
# 做法：从本文件所在目录逐级向上，找到**第一个含 fastdl.py 的目录**，
# 并以最高优先级插入 sys.path。
#
# 为什么不能简单地"把父目录也加进去"：
#   若父目录里恰好有个同名的无关 fastdl.py（很常见，比如工作区根目录），
#   就会导入到错误的模块。只认最近的那个，避免误伤。
def _find_fastdl_dir(start: str, max_up: int = 4):
    """从 start 向上查找含 fastdl.py 的最近目录，找不到返回 None。"""
    cur = start
    for _ in range(max_up + 1):
        if os.path.isfile(os.path.join(cur, "fastdl.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:  # 已到根
            break
        cur = parent
    return None


_DIR = _find_fastdl_dir(os.path.dirname(os.path.abspath(__file__)))
if _DIR is None:
    raise ImportError(
        "找不到 fastdl.py。请把它与本测试放在同一目录，"
        "或保持仓库结构（tests/ 与 fastdl.py 同级）。"
    )
sys.path.insert(0, _DIR)

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


class TestAnonymousApiAccess(unittest.TestCase):
    """公开仓库无需认证：没装 gh、也没传 token 时不能直接报错。

    历史问题（新用户第一眼就会撞上）：gh_api 在 gh CLI 不可用且无 token 时
    直接抛 RuntimeError("无法调用 gh CLI，请安装 gh 并登录，或传入 --token")。
    但下载**公开** release 根本不需要认证，这句话纯属误导。
    现改为：无 token 时走匿名 API 请求。
    """

    class _FakeResp:
        def __init__(self, payload=b'{"ok": true}'):
            self._p = payload

        def read(self):
            return self._p

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _patch(self, captured):
        from unittest import mock

        class FakeOpener:
            def open(self, req, timeout=None):
                captured.append(req)
                return TestAnonymousApiAccess._FakeResp()

        return mock.patch.object(
            fastdl.urllib.request, "build_opener",
            return_value=FakeOpener(),
        )

    def test_no_token_still_queries_api(self):
        """gh 不可用 + 无 token → 仍应发起匿名请求，而不是提前报错。"""
        from unittest import mock
        captured = []
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("gh")), \
                self._patch(captured):
            out = fastdl.gh_api("repos/a/b/releases/tags/v1", None)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(captured), 1, "应当发出了一次匿名请求")

    def test_anonymous_request_sends_no_credentials(self):
        """匿名请求绝不能携带 Authorization 头。"""
        from unittest import mock
        captured = []
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("gh")), \
                self._patch(captured):
            fastdl.gh_api("repos/a/b/releases/tags/v1", None)
        self.assertIsNone(captured[0].get_header("Authorization"))

    def test_token_is_sent_when_provided(self):
        """给了 token 才带 Authorization 头。"""
        from unittest import mock
        captured = []
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("gh")), \
                self._patch(captured):
            fastdl.gh_api("repos/a/b/releases/tags/v1", "ghp_x")
        self.assertEqual(captured[0].get_header("Authorization"), "token ghp_x")

    def test_gh_cli_preferred_when_available(self):
        """gh CLI 可用时应优先用它（免 token、配额更高）。"""
        from unittest import mock

        class R:
            returncode = 0
            stdout = '{"from": "cli"}'

        captured = []
        with mock.patch("subprocess.run", return_value=R()), self._patch(captured):
            out = fastdl.gh_api("repos/a/b/releases/tags/v1", None)
        self.assertEqual(out, {"from": "cli"})
        self.assertEqual(captured, [], "gh 可用时不应再走 urllib")


class TestListAssetsCli(unittest.TestCase):
    """--list-assets 的参数校验（不触网）。"""

    def _err(self, argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit) as cm:
                fastdl.main(argv)
        self.assertEqual(cm.exception.code, 2)
        return buf.getvalue()

    def test_requires_gh_release_and_tag(self):
        self.assertIn("--list-assets", self._err(["--list-assets"]))
        self.assertIn("--list-assets", self._err(["--list-assets",
                                                  "--gh-release", "a/b"]))


class TestGithubUrlHint(unittest.TestCase):
    """GitHub 网页链接要给出可操作提示，而不是底层报错。

    历史问题：把 release 页面地址粘进来，会一路走到 probe_size，
    GitHub 对 HTML 返回 `Content-Range: bytes 0-0/*`，
    int('*') 直接抛 ValueError，用户看到一堆 Python 堆栈。
    """

    def test_direct_asset_url_is_allowed_through(self):
        """真正的直链必须放行，否则正常下载会被拦下。"""
        for u in (
            "https://github.com/cli/cli/releases/download/v2.10.0/x.zip",
            "https://example.com/big.zip",
            "https://objects.githubusercontent.com/abc/file.zip",
        ):
            self.assertIsNone(fastdl.github_url_hint(u), "不应拦截直链：%s" % u)

    def test_release_page_suggests_gh_release(self):
        h = fastdl.github_url_hint("https://github.com/cli/cli/releases/tag/v2.10.0")
        self.assertIsNotNone(h)
        self.assertIn("--gh-release cli/cli", h)
        self.assertIn("--tag v2.10.0", h)

    def test_repo_home_suggests_gh_release(self):
        for u in ("https://github.com/cli/cli",
                  "https://github.com/cli/cli.git",
                  "https://github.com/cli/cli/"):
            h = fastdl.github_url_hint(u)
            self.assertIsNotNone(h, "应拦截仓库主页：%s" % u)
            self.assertIn("cli/cli", h)

    def test_releases_index_asks_for_tag(self):
        for u in ("https://github.com/cli/cli/releases",
                  "https://github.com/cli/cli/releases/latest"):
            h = fastdl.github_url_hint(u)
            self.assertIsNotNone(h, "应拦截 release 列表页：%s" % u)
            self.assertIn("--tag", h)

    def test_code_browse_links_detected(self):
        for u in ("https://github.com/cli/cli/archive/refs/tags/v2.10.0.tar.gz",
                  "https://github.com/cli/cli/tree/trunk",
                  "https://github.com/cli/cli/blob/trunk/README.md"):
            self.assertIsNotNone(fastdl.github_url_hint(u),
                                 "应拦截代码浏览链接：%s" % u)

    def test_query_and_fragment_do_not_confuse(self):
        u = "https://github.com/cli/cli/releases/tag/v2.10.0?tab=readme#notes"
        h = fastdl.github_url_hint(u)
        self.assertIsNotNone(h)
        self.assertIn("v2.10.0", h)


class TestContentRangeParsing(unittest.TestCase):
    """Content-Range 解析必须容错。

    历史问题：`bytes 0-0/*`（GitHub 对 HTML 页面返回）会 int('*') 崩溃。
    """

    def test_known_total(self):
        self.assertEqual(fastdl._parse_total("bytes 0-0/12345"), 12345)

    def test_unknown_total_star(self):
        """`*` 表示总长未知，应返回 None 而不是崩溃。"""
        self.assertIsNone(fastdl._parse_total("bytes 0-0/*"))

    def test_unsatisfied_range_carries_known_total(self):
        """RFC 7233：416 响应用 `bytes */<total>` 表达总长度。

        此时总长是**已知**的 123，解析出来是对的。
        （实践中 urllib 会对 416 抛 HTTPError，一般到不了这里，
        但语义正确性应当保持。）
        """
        self.assertEqual(fastdl._parse_total("bytes */123"), 123)

    def test_malformed_inputs(self):
        for bad in ("", "bytes 0-0", "garbage", "bytes 0-0/abc", "bytes x-y/z"):
            self.assertIsNone(fastdl._parse_total(bad),
                              "畸形输入应返回 None：%r" % bad)


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


class TestLimitMb(unittest.TestCase):
    """--limit-mb：只下载前 N MiB（试水 / 演示做公平对比用）。

    契约：
      · 产物恰好 N 字节
      · 内容等于源文件的**前 N 字节**（逐字节一致）
      · 既然是截断文件，就必须跳过 sha256 校验，否则必然失败

    这几个约束一旦破坏，演示脚本里「两种方式传输相同字节数」的前提就没了，
    对比也就不公平了。
    """

    DATA = bytes((i * 13 + 7) % 256 for i in range(3 * 1024 * 1024))
    URL = "https://example.com/big.bin"

    def _run(self, argv):
        from unittest import mock
        opener = FakeOpener(self.DATA)
        with mock.patch.object(fastdl, "build_opener", return_value=opener), \
                mock.patch.object(fastdl, "probe_size",
                                  return_value=(len(self.DATA), True)):
            rc = fastdl.main(argv)
        return rc

    def _tmp_out(self, name="out.bin"):
        tmp = tempfile.mkdtemp(prefix="fastdl_limit_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return os.path.join(tmp, name)

    def test_clamps_to_exactly_n_bytes(self):
        out = self._tmp_out()
        rc = self._run([self.URL, "-o", out, "--limit-mb", "1",
                        "-c", "1", "-t", "4"])
        self.assertEqual(rc, 0)
        self.assertEqual(os.path.getsize(out), 1024 * 1024)

    def test_clamped_content_is_prefix(self):
        """截断内容必须精确等于源文件前 N 字节。"""
        out = self._tmp_out()
        self._run([self.URL, "-o", out, "--limit-mb", "1", "-c", "1", "-t", "4"])
        with open(out, "rb") as f:
            got = f.read()
        self.assertEqual(got, self.DATA[:1024 * 1024])

    def test_skips_sha256_when_clamped(self):
        """给了完整文件的 digest 也不该校验失败——截断时必须跳过。"""
        import hashlib
        full = hashlib.sha256(self.DATA).hexdigest()
        out = self._tmp_out()
        rc = self._run([self.URL, "-o", out, "--limit-mb", "1", "-c", "1",
                        "-t", "4", "--sha256", full])
        self.assertEqual(rc, 0, "截断时应跳过 sha256，不该返回校验失败(3)")

    def test_limit_larger_than_file_downloads_whole(self):
        """--limit-mb 超过文件大小时，应下载完整文件并正常校验。"""
        import hashlib
        full = hashlib.sha256(self.DATA).hexdigest()
        out = self._tmp_out()
        rc = self._run([self.URL, "-o", out, "--limit-mb", "99", "-c", "1",
                        "-t", "4", "--sha256", full])
        self.assertEqual(rc, 0)
        self.assertEqual(os.path.getsize(out), len(self.DATA))


class TestPartCountForDemo(unittest.TestCase):
    """演示脚本的前提：样本必须能切出多片，否则并发无从体现。

    默认分片 8 MiB 配 4 MiB 样本只会切出 1 片 = 单连接，
    演示会得出「没有加速」的错误结论。这里把这个前提钉住。
    """

    def test_default_chunk_would_starve_small_sample(self):
        size, default_chunk = 4 * 1024 * 1024, 8 * 1024 * 1024
        n = (size + default_chunk - 1) // default_chunk
        self.assertEqual(n, 1, "默认 8MiB 分片配 4MiB 样本确实只有 1 片")

    def test_small_chunk_yields_multiple_parts(self):
        size, chunk = 4 * 1024 * 1024, 1 * 1024 * 1024
        n = (size + chunk - 1) // chunk
        self.assertEqual(n, 4, "1MiB 分片应切出 4 片")


if __name__ == "__main__":
    unittest.main(verbosity=2)
