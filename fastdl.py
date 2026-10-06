#!/usr/bin/env python3
"""fastdl - 并行分片下载器

单连接下载大文件常被服务端限速到几十KB/s。本工具用N 个线程并发拉取
不同字节区间，把带宽利用率拉满。附带断点续传与 sha256 校验。

仅用标准库，无需pip install。

用法:
    # 直接给 URL（自动探测大小，不校验）
    python fastdl.py https://github.com/owner/repo/releases/download/v1/file.zip

    # 指定输出名+ 线程数
    python fastdl.py -o out.zip -t 32<URL>

    # 带校验（强烈建议）
    python fastdl.py --sha256 ba75cb33... <URL>

    # GitHub release：自动读asset 列表和官方 sha256
    python fastdl.py --gh-release owner/repo <tag>

断点续传：中断后重跑同一条命令即可，已下载分片不会重复下载。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import unquote

__version__ = "1.1.0"

DEFAULT_THREADS = 16
DEFAULT_CHUNK = 8 * 1024 * 1024# 8 MiB
DEFAULT_RETRIES = 6
USER_AGENT = "fastdl/%s (+https://github.com/joker1point/gh-fast-download)" % __version__


def _force_utf8_stdio() -> None:
    """把标准输出切到 UTF-8。

    Windows 控制台默认编码是 cp1252/cp936，无法编码部分中文字符，
    打印 --help 或进度时会抛 UnicodeEncodeError。这里统一强制 UTF-8，
    并对无法重配的流做降级（errors='replace'），保证只输出不崩。

    在**导入时**执行，这样单元测试（import fastdl）也能受益——
    放在 __main__ 里只对直接运行生效，测试环境会漏。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # Python < 3.7 或流不支持重配：退而求其次，容忍编码错误
            try:
                stream.reconfigure(errors="replace")
            except Exception:  # noqa: BLE001
                pass


_force_utf8_stdio()


# ---------------------------------------------------------------- 输出helpers

def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%.1f PiB" % n


class Printer:
    """单行进度输出；非TTY 时降级为周期性换行，避免刷屏。"""

    def __init__(self):
        self.tty = sys.stdout.isatty()
        self._lock = threading.Lock()
        self._last = 0.0

    def progress(self, done: int, total: int, rate: float, force: bool = False):
        now = time.perf_counter()
        with self._lock:
            if not self.tty and not force and now - self._last < 15:
                return
            self._last = now
            pct = done * 100.0 / total if total else 0.0
            eta = (total - done) / rate if rate > 0 else float("inf")
            msg = "[%5.1f%%] %s / %s  %s/s  ETA %s" % (
                pct, human(done), human(total), human(rate),
                ("%.0f min" % (eta / 60)) if eta != float("inf") else "--",
            )
            if self.tty:
                sys.stdout.write("\r" + msg.ljust(72))
            else:
                sys.stdout.write(msg + "\n")
            sys.stdout.flush()

    def done(self):
        if self.tty:
            sys.stdout.write("\n")
            sys.stdout.flush()

    def info(self, msg: str):
        with self._lock:
            if self.tty:
                sys.stdout.write("\r" + " " * 72 + "\r")
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()


# ---------------------------------------------------------------- 网络helpers

def build_opener(no_tls_verify: bool, use_proxy: bool):
    """构造 opener。

    no_tls_verify  :对应 curl --ssl-no-revoke，绕开 Windows schannel 的
                      证书吊销检查失败（CRYPT_E_NO_REVOCATION_CHECK / curl exit 35）。
    use_proxy       : 默认 False。实测本机代理往往比直连慢数倍。
    """
    handlers = []
    if not use_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    if no_tls_verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


# 关闭 TLS 校验时又携带凭据，是明确的中间人风险，必须显式告警。
INSECURE_CREDENTIAL_WARNING = """\
⚠️  安全警告：已关闭 TLS 证书校验（--no-tls-verify），同时检测到凭据（token）。
    此组合下中间人可窃取你的 token —— 关闭校验后无法识别假冒的服务器。
    建议：
      · 私有仓库优先用 `gh auth login` 让 gh CLI 处理认证，避免传 --token；
      · 或改用 SSH 拉取，绕开 HTTPS token；
      · 仅在确认是 Windows schannel 吊销检查问题时才临时关闭校验，
        且不要与 --token 同时使用。"""


def warn_insecure_credential(no_tls_verify: bool, token: str | None) -> bool:
    """在「关闭 TLS 校验 + 携带凭据」时发出告警。

    返回 True 表示确实触发了告警（便于测试与调用方判断）。
    """
    if no_tls_verify and token:
        print(INSECURE_CREDENTIAL_WARNING, file=sys.stderr, flush=True)
        return True
    return False


def _parse_total(cr: str):
    """从 Content-Range 解析总长度。

    `bytes 0-0/12345` → 12345
    `bytes 0-0/*`     → None（总长未知）

    注意 `*` 的情况：GitHub 对 HTML 页面（仓库页 / release 页）会返回
    `Content-Range: bytes 0-0/*`。早期版本直接 int() 会抛
    `ValueError: invalid literal for int() with base 10: '*'`，
    用户粘错链接就看到一堆 Python 堆栈。
    """
    if not cr or "/" not in cr:
        return None
    total = cr.rsplit("/", 1)[-1].strip()
    return int(total) if total.isdigit() else None


def probe_size(opener, url: str, token: str | None):
    """用 Range 请求探测大小与是否支持断点续传。

    返回 (total_size, accept_ranges)。
    服务端不支持分片、或总长未知时 total 为 None。
    """
    req = urllib.request.Request(url)
    req.add_header("Range", "bytes=0-0")
    req.add_header("User-Agent", USER_AGENT)
    if token:
        req.add_header("Authorization", "token " + token)
    with opener.open(req, timeout=30) as r:
        status = r.status
        cr = r.headers.get("Content-Range")
        r.read(1)
    total = _parse_total(cr)
    if total is not None:
        return total, True
    if cr:
        # 有 Content-Range 但总长是 `*`：多为网页响应，无法分片下载
        return None, False
    # 无 Content-Range：退回 HEAD
    try:
        req2 = urllib.request.Request(url, method="HEAD")
        req2.add_header("User-Agent", USER_AGENT)
        if token:
            req2.add_header("Authorization", "token " + token)
        with opener.open(req2, timeout=30) as r2:
            clen = r2.headers.get("Content-Length")
            size = int(clen) if (clen or "").isdigit() else None
            return size, status == 200
    except Exception:
        return None, False


def sha256_file(path: str, bufsize: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(bufsize)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


# ---------------------------------------------------------------- GitHub 集成

# 匹配 GitHub 网页/仓库链接（直链会带 releases/download/，另行放过）
_GH_WEB_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/?#]+)/(?P<repo>[^/?#]+?)"
    r"(?:\.git)?(?:/(?P<rest>[^?#]*))?/?$"
)


def github_url_hint(url: str):
    """若给的是 GitHub **网页**链接而非可下载直链，返回可操作的提示。

    新用户最常见的一步操作失误，就是把 release 页面或仓库主页地址
    直接粘进来。此时若只抛一个底层报错（如 ValueError），用户完全
    不知道该怎么办。这里直接把「应该改用哪条命令」写清楚。

    返回 None 表示链接看起来是可下载直链，或不是 GitHub 链接。
    """
    m = _GH_WEB_RE.match(url.split("?")[0].split("#")[0])
    if not m:
        return None
    owner, repo = m.group("owner"), m.group("repo")
    rest = (m.group("rest") or "").strip("/")

    if rest.startswith("releases/download/"):
        return None  # 合法直链，交给正常流程
    if not rest:
        return ("%s/%s 是仓库主页，不是可下载的直链。\n"
                "  下载它的 release：--gh-release %s/%s --tag <tag>\n"
                "  （想看有哪些 tag，可先访问该仓库的 Releases 页面）"
                % (owner, repo, owner, repo))
    if rest.startswith("releases/tag/"):
        tag = rest[len("releases/tag/"):]
        return ("这是 release **页面**链接，不是可下载的直链。\n"
                "  改用：--gh-release %s/%s --tag %s\n"
                "  （该 release 有多个资产时，再加 --asset <文件名>）"
                % (owner, repo, tag))
    if rest in ("releases", "releases/latest"):
        return ("这是 release 列表页，需要指明具体版本：\n"
                "  --gh-release %s/%s --tag <tag>" % (owner, repo))
    if rest.startswith(("archive/", "tree/", "blob/")) or rest in ("archive", "tree", "blob"):
        return ("这是代码浏览 / 源码包链接，不是 release 资产。\n"
                "  想下载源码压缩包，建议直接 `git clone`；\n"
                "  若目标是 release 资产，请用 --gh-release %s/%s --tag <tag>"
                % (owner, repo))
    return None


def gh_api(path: str, token: str | None, no_tls_verify: bool = False):
    """调用 GitHub API。优先用 gh CLI（已登录时免 token），否则用 urllib。

    安全要点：`no_tls_verify` **默认关闭证书校验豁免**。
    本函数在携带 Authorization 头时会发送凭据，若同时关闭 TLS 校验，
    中间人即可窃取 token。因此只在调用方显式要求时才放宽校验。
    （早期版本此处无条件设 CERT_NONE，属安全缺陷，已修正。）
    """
    import subprocess

    try:
        out = subprocess.run(
            ["gh", "api", path, "--jq", "."],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout)
    except Exception:
        pass
    # 回退到 urllib。
    #
    # 关键：**公开仓库无需认证**，所以 token 是可选的。
    # 没有 token 就走匿名请求——GitHub 允许匿名访问 API
    # （限速 60 次/小时，但取一次 release 元数据绰绰有余）。
    # 早期版本在这里直接报"请安装 gh 并登录或传 --token"，
    # 对只想下个公开文件的用户是纯误导，已修正。
    req = urllib.request.Request("https://api.github.com/" + path)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", "token " + token)

    if no_tls_verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        https_handler = urllib.request.HTTPSHandler(context=ctx)
    else:
        https_handler = urllib.request.HTTPSHandler()  # 默认：正常校验证书
    op = urllib.request.build_opener(
        https_handler,
        urllib.request.ProxyHandler({}),
    )
    try:
        with op.open(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404 and not token:
            raise RuntimeError(
                "GitHub API 返回 404：仓库或 tag 不存在，"
                "也可能这是**私有仓库**、需要认证。\n"
                "  · 公开仓库无需任何认证，请先核对 owner/repo 与 tag 是否正确；\n"
                "  · 私有仓库请用 `gh auth login`（推荐）或传 --token。"
            )
        if e.code == 403 and not token:
            raise RuntimeError(
                "GitHub API 返回 403，通常是匿名请求触发了限速"
                "（每小时 60 次）。\n"
                "  · 稍后重试；或 `gh auth login` / 传 --token 以提高配额。"
            )
        raise


def list_gh_release_assets(repo: str, tag: str, token: str | None,
                           no_tls_verify: bool = False) -> None:
    """打印某个 release 的全部资产，供用户决定 --asset 填什么。

    新用户最常卡住的一步就是"资产名到底叫什么"。与其让他们去网页上
    一个个抄，不如直接列出来。
    """
    data = gh_api("repos/%s/releases/tags/%s" % (repo, tag), token, no_tls_verify)
    assets = data.get("assets") or []
    print("%s @ %s  共 %d 个资产" % (repo, tag, len(assets)))
    if not assets:
        print("（该 release 没有可下载资产，可能只有源码压缩包）")
        return
    print("")
    print("  %-44s %12s" % ("名称（--asset 用这个）", "大小"))
    print("  " + "-" * 58)
    for a in assets:
        has_sha = "sha256" if (a.get("digest") or "").startswith("sha256:") else "—"
        print("  %-44s %12s  %s" % (a["name"], human(int(a["size"])), has_sha))
    print("")
    print("用法：--gh-release %s --tag %s --asset <上面的名称>" % (repo, tag))


def resolve_gh_release(repo: str, tag: str, token: str | None, asset: str | None,
                       no_tls_verify: bool = False):
    """把 owner/repo + tag 解析成 (url, size, sha256, name)。"""
    data = gh_api("repos/%s/releases/tags/%s" % (repo, tag), token, no_tls_verify)
    assets = data.get("assets") or []
    if not assets:
        raise RuntimeError("release %s 下没有可下载资产" % tag)
    picked = None
    if asset:
        for a in assets:
            if a["name"] == asset:
                picked = a
                break
        if picked is None:
            names = "\n  ".join(a["name"] for a in assets)
            raise RuntimeError(
                "找不到资产 %r。该 release 的可选资产：\n  %s\n"
                "（提示：加 --list-assets 可随时列出）" % (asset, names)
            )
    else:
        if len(assets) > 1:
            names = "\n  ".join(a["name"] for a in assets)
            raise RuntimeError(
                "该 release 有多个资产，请用 --asset 指定其一：\n  %s\n"
                "（提示：加 --list-assets 可随时列出）" % names
            )
        picked = assets[0]
    digest = picked.get("digest") or ""
    sha = digest.split("sha256:")[-1] if digest.startswith("sha256:") else None
    return picked["browser_download_url"], int(picked["size"]), sha, picked["name"]


# ---------------------------------------------------------------- 下载核心

class Downloader:
    def __init__(self, url, out, size, sha256, threads, chunk, retries,
                 opener, token=None, keep_parts=False):
        self.url = url
        self.out = out
        self.size = size
        self.sha256 = (sha256 or "").lower() or None
        self.threads = max(1, threads)
        self.chunk = chunk
        self.retries = retries
        self.opener = opener
        self.token = token
        self.keep_parts = keep_parts
        self.part_dir = out + ".parts"
        self.n_parts = (size + chunk - 1) // chunk
        self._fail = []
        self._flock = threading.Lock()

    def part_path(self, i: int) -> str:
        return os.path.join(self.part_dir, "p%04d" % i)

    def part_size(self, i: int) -> int:
        p = self.part_path(i)
        return os.path.getsize(p) if os.path.exists(p) else 0

    def done_bytes(self) -> int:
        return sum(self.part_size(i) for i in range(self.n_parts))

    def _fetch(self, idx: int) -> bool:
        start = idx * self.chunk
        end = min(start + self.chunk, self.size) - 1
        want = end - start + 1
        have = self.part_size(idx)
        if have >= want:
            return True

        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(self.url)
                req.add_header("Range", "bytes=%d-%d" % (start + have, end))
                req.add_header("User-Agent", USER_AGENT)
                if self.token:
                    req.add_header("Authorization", "token " + self.token)
                mode = "ab" if have else "wb"
                with self.opener.open(req, timeout=60) as r, \
                        open(self.part_path(idx), mode) as f:
                    while True:
                        buf = r.read(262144)
                        if not buf:
                            break
                        f.write(buf)
                        have += len(buf)
                if have >= want:
                    return True
            except Exception as e:  # noqa: BLE001 - 网络异常种类繁多，统一重试
                if attempt == self.retries - 1:
                    with self._flock:
                        self._fail.append((idx, str(e)))
                    return False
                time.sleep(1.5 * (attempt + 1))
        return False

    def run(self) -> int:
        os.makedirs(self.part_dir, exist_ok=True)
        pre = self.done_bytes()
        print("分片数 %d  线程 %d  已有 %.1f MiB (%.1f%%)"
              % (self.n_parts, self.threads, pre / 1048576, pre * 100.0 / self.size),
              flush=True)

        # 参数明显过头时提醒：并发收益来自传输时间，不是请求数量。
        # 线程数远超分片数 = 大量空转线程；文件太小则传输时间不足以摊薄握手。
        if self.threads > max(self.n_parts, 1) * 2:
            print("提示：线程数(%d) 远超分片数(%d)，多数线程会空等，"
                  "建议 -t %d 左右。" % (self.threads, self.n_parts,
                                       max(2, self.n_parts)), flush=True)
        elif self.size < 20 * 1048576 and self.threads > 8:
            print("提示：文件仅 %.1f MiB，高并发收益有限（时间会耗在握手而非传输），"
                  "建议用默认值或更小的 -t。" % (self.size / 1048576), flush=True)

        t0 = time.perf_counter()
        p = Printer()
        with concurrent.futures.ThreadPoolExecutor(self.threads) as ex:
            futs = [ex.submit(self._fetch, i) for i in range(self.n_parts)]
            while True:
                n_done = sum(f.done() for f in futs)
                cur = self.done_bytes()
                el = max(time.perf_counter() - t0, 0.001)
                p.progress(cur, self.size, (cur - pre) / el)
                if n_done == len(futs):
                    break
                time.sleep(2)
        p.done()

        if self._fail:
            print("以下分片最终失败：", file=sys.stderr)
            for idx, err in self._fail[:20]:
                print("  p%04d: %s" % (idx, err), file=sys.stderr)
            print("修复网络后重跑同一条命令即可续传。", file=sys.stderr)
            return 2

        print("合并分片...", flush=True)
        with open(self.out, "wb") as out:
            for i in range(self.n_parts):
                with open(self.part_path(i), "rb") as f:
                    shutil.copyfileobj(f, out, 1 << 20)

        actual = os.path.getsize(self.out)
        print("大小 %d（期望 %d）" % (actual, self.size), flush=True)
        if actual != self.size:
            print("大小不匹配 ✗", file=sys.stderr)
            return 4

        if self.sha256:
            print("计算 sha256...", flush=True)
            got = sha256_file(self.out)
            print("sha256 %s" % got, flush=True)
            if got != self.sha256:
                print("校验失败 ✗ 期望 %s" % self.sha256, file=sys.stderr)
                print("分片已保留供重试：%s" % self.part_dir, file=sys.stderr)
                return 3
            print("校验通过 ✓", flush=True)
        else:
            print("未提供期望 sha256，跳过校验。", flush=True)

        # 收尾清理必须非致命：清理失败只警告，不影响退出码。
        if self.keep_parts:
            print("KEEP_PARTS：保留分片目录 %s" % self.part_dir, flush=True)
        else:
            try:
                shutil.rmtree(self.part_dir)
            except Exception as e:  # noqa: BLE001
                print("提示：分片目录清理失败（%s），可手动删除 %s"
                      % (e, self.part_dir), flush=True)
        return 0


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="fastdl",
        description="并行分片下载器：多连接突破单连接限速，支持断点续传与 sha256 校验。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("url", nargs="?", help="下载地址")
    ap.add_argument("-o", "--out", help="输出文件路径（默认取URL 最后一段）")
    ap.add_argument("-t", "--threads", type=int, default=DEFAULT_THREADS,
                    help="并发线程数（默认 %d）" % DEFAULT_THREADS)
    ap.add_argument("-c", "--chunk-mb", type=int, default=DEFAULT_CHUNK // 1048576,
                    help="分片大小 MiB（默认 %d）" % (DEFAULT_CHUNK // 1048576))
    ap.add_argument("--sha256", help="期望的 sha256 十六进制串")
    ap.add_argument("--gh-release", metavar="OWNER/REPO",
                    help="从 GitHub release 下载，自动取 size 与官方 sha256")
    ap.add_argument("--tag", help="配合 --gh-release 使用的 tag")
    ap.add_argument("--asset", help="配合 --gh-release 指定资产名（多资产时必填）")
    ap.add_argument("--list-assets", action="store_true",
                    help="只列出该 release 的所有资产及大小，不下载"
                         "（忘了资产名时用这个）")
    ap.add_argument("--token", help="私有仓库 token（也可用环境变量 GITHUB_TOKEN）。"
                                    "注意：不要与 --no-tls-verify 同时使用")
    ap.add_argument("--no-tls-verify", action="store_true",
                    help="关闭证书校验，等价 curl --ssl-no-revoke"
                         "（Windows schannel 吊销检查失败时需要）。"
                         "会失去防中间人能力，勿与 --token 同用")
    ap.add_argument("--use-proxy", action="store_true",
                    help="走系统代理（默认直连；实测代理常更慢）")
    ap.add_argument("-k", "--keep-parts", action="store_true",
                    help="完成后保留分片目录")
    ap.add_argument("-V", "--version", action="version",
                    version="fastdl " + __version__)
    args = ap.parse_args(argv)

    token = args.token or os.environ.get("GITHUB_TOKEN")
    warn_insecure_credential(args.no_tls_verify, token)

    # --list-assets：只列资产，不下载。用来回答"资产名到底叫什么"。
    if args.list_assets:
        if not args.gh_release or not args.tag:
            ap.error("--list-assets 需要配合 --gh-release OWNER/REPO --tag TAG")
        list_gh_release_assets(args.gh_release, args.tag, token,
                               args.no_tls_verify)
        return 0

    url, size, sha, name = args.url, None, args.sha256, None
    if args.gh_release:
        if not args.tag:
            # 高频误用：`--gh-release owner/repo v1.0.0`（漏了 --tag）
            if args.url:
                ap.error(
                    "检测到你可能想写 `--gh-release %s %s`——"
                    "tag 必须用 --tag 传递。\n"
                    "正确写法：--gh-release %s --tag %s"
                    % (args.gh_release, args.url, args.gh_release, args.url)
                )
            ap.error("--gh-release 需要配合 --tag TAG")
        url, size, sha, name = resolve_gh_release(
            args.gh_release, args.tag, token, args.asset, args.no_tls_verify)
        print("资产 %s  %s" % (name, human(size)), flush=True)
    elif args.url and args.tag:
        # 反向误用：给了 --tag 却没给 --gh-release
        ap.error("--tag 只能配合 --gh-release 使用")
    if not url:
        ap.error("需要 url 或 --gh-release OWNER/REPO --tag TAG")

    # 粘了 GitHub 网页链接（仓库页 / release 页）是最常见的新手失误。
    # 与其让它一路走到 probe_size 抛底层异常，不如在这里直接说清怎么改。
    hint = github_url_hint(url)
    if hint:
        ap.error(hint)

    out = args.out
    if not out:
        base = url.split("?")[0].rstrip("/").split("/")[-1]
        base = unquote(base)
        out = os.path.join(os.getcwd(), base or "download.bin")

    opener = build_opener(args.no_tls_verify, args.use_proxy)

    if size is None:
        size, ranged = probe_size(opener, url, token)
        if not size:
            print("错误：无法确定文件大小，也就无法分片下载。", file=sys.stderr)
            print("  常见原因：链接指向的是网页（仓库页 / release 页）"
                  "而不是文件本身，", file=sys.stderr)
            print("  或该地址不支持 Range / 需要登录。", file=sys.stderr)
            print("  建议：到 release 页面右键复制资产的**真实下载地址**"
                  "（形如 .../releases/download/<tag>/<文件名>），", file=sys.stderr)
            print("        或改用 --gh-release <owner>/<repo> --tag <tag>。",
                  file=sys.stderr)
            return 5
        if not ranged:
            print("警告：服务端不支持 Range 请求，断点续传不可用，"
                  "并发加速效果也会受限。", file=sys.stderr)

    dl = Downloader(
        url=url, out=out, size=size, sha256=sha, threads=args.threads,
        chunk=max(1, args.chunk_mb) * 1048576, retries=DEFAULT_RETRIES,
        opener=opener, token=token, keep_parts=args.keep_parts,
    )
    return dl.run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。分片已保留，重跑同一条命令即可续传。", file=sys.stderr)
        sys.exit(130)
    except RuntimeError as e:
        # 参数/远端数据这类用户可自行纠正的问题，给简洁提示而非 traceback
        print("错误：%s" % e, file=sys.stderr)
        sys.exit(1)
    except urllib.error.HTTPError as e:
        print("HTTP 错误：%s %s" % (e.code, e.reason), file=sys.stderr)
        if e.code in (401, 403):
            print("可能是私有仓库未认证或 token 无权限；"
                  "私有仓库请用 `gh auth login` 或传 --token。", file=sys.stderr)
        elif e.code == 404:
            print("资源不存在：请检查 owner/repo、tag、资产名是否正确。",
                  file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print("网络错误：%s" % e.reason, file=sys.stderr)
        print("请检查网络连通性。若为证书问题，可尝试 --no-tls-verify。",
              file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        # 磁盘写满、权限不足、路径非法等
        print("系统错误：%s" % e, file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        # 兜底：解析远端返回内容时出错（如异常的 Content-Range）。
        # 这类问题不该以裸 traceback 的形式甩给用户。
        print("解析远端响应失败：%s" % e, file=sys.stderr)
        print("请确认 URL 指向的是文件本身而非网页；"
              "若问题持续，欢迎反馈："
              "https://github.com/joker1point/gh-fast-download/issues",
              file=sys.stderr)
        sys.exit(1)
