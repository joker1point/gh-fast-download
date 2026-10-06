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
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import unquote

__version__ = "1.0.0"

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


def probe_size(opener, url: str, token: str | None):
    """用 Range 请求探测大小与是否支持断点续传。

    返回 (total_size, accept_ranges)。服务端忽略 Range 时 total 为 None。
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
    if cr and "/" in cr:
        return int(cr.split("/")[-1]), True
    # 无 Content-Range：退回 HEAD
    try:
        req2 = urllib.request.Request(url, method="HEAD")
        req2.add_header("User-Agent", USER_AGENT)
        if token:
            req2.add_header("Authorization", "token " + token)
        with opener.open(req2, timeout=30) as r2:
            return int(r2.headers.get("Content-Length", 0)), status == 200
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

def gh_api(path: str, token: str | None):
    """调用 GitHub API。优先用 gh CLI（已登录时免token），否则用 urllib。"""
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
    if not token:
        raise RuntimeError("无法调用 gh CLI，请安装 gh 并登录，或传入 --token")
    req = urllib.request.Request("https://api.github.com/" + path)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", "token " + token)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    op = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.ProxyHandler({}),
    )
    with op.open(req, timeout=60) as r:
        return json.loads(r.read().decode())


def resolve_gh_release(repo: str, tag: str, token: str | None, asset: str | None):
    """把 owner/repo + tag 解析成 (url, size, sha256, name)。"""
    data = gh_api("repos/%s/releases/tags/%s" % (repo, tag), token)
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
            names = ", ".join(a["name"] for a in assets)
            raise RuntimeError("找不到资产 %r。可选：%s" % (asset, names))
    else:
        if len(assets) > 1:
            names = ", ".join(a["name"] for a in assets)
            raise RuntimeError("该 release 有多个资产，请用 --asset指定：%s" % names)
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
    ap.add_argument("--token", help="私有仓库 token（也可用环境变量 GITHUB_TOKEN）")
    ap.add_argument("--no-tls-verify", action="store_true",
                    help="关闭证书校验，等价 curl --ssl-no-revoke"
                         "（Windows schannel 吊销检查失败时需要）")
    ap.add_argument("--use-proxy", action="store_true",
                    help="走系统代理（默认直连；实测代理常更慢）")
    ap.add_argument("-k", "--keep-parts", action="store_true",
                    help="完成后保留分片目录")
    ap.add_argument("-V", "--version", action="version",
                    version="fastdl " + __version__)
    args = ap.parse_args(argv)

    token = args.token or os.environ.get("GITHUB_TOKEN")

    url, size, sha, name = args.url, None, args.sha256, None
    if args.gh_release:
        if not args.tag:
            ap.error("--gh-release 需要配合 --tag")
        url, size, sha, name = resolve_gh_release(
            args.gh_release, args.tag, token, args.asset)
        print("资产 %s  %s" % (name, human(size)), flush=True)
    if not url:
        ap.error("需要 url 或 --gh-release OWNER/REPO --tag TAG")

    out = args.out
    if not out:
        base = url.split("?")[0].rstrip("/").split("/")[-1]
        base = unquote(base)
        out = os.path.join(os.getcwd(), base or "download.bin")

    opener = build_opener(args.no_tls_verify, args.use_proxy)

    if size is None:
        size, ranged = probe_size(opener, url, token)
        if not size:
            print("无法确定文件大小，服务端可能不支持 HEAD", file=sys.stderr)
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
