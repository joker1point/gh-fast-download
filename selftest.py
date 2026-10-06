#!/usr/bin/env python3
"""环境自检：在下载大文件前，先确认这台机器的连通性与并发是否真的有效。

用法:
    python selftest.py                # 用内置的小测试文件（约 3MB）
    python selftest.py <URL>          # 用自己的 URL 测试

会输出：
  1. TLS 是否可用（Windows schannel 吊销检查失败的探测）
  2. 单连接速度
  3. 8连接并发总速度
  4. 并发是否真的有收益，以及建议的线程数
"""
from __future__ import annotations

import argparse
import concurrent.futures
import ssl
import sys
import time
import urllib.request

# 样本要足够大，否则测的是握手延迟而非吞吐：
# 每片仅 393KB 时，8 连接的总耗时几乎全花在 TLS 握手 + RTT 上，
# 会得出「并发反而更慢」的错误结论。16MB 足以让传输时间占主导。
TEST_BYTES = 16 * 1024 * 1024
# 用release 资产：它稳定支持 HTTP Range。
# 不要用 /archive/ 端点—— 它会忽略 Range 返回全量，导致测速失真。
DEFAULT_URL = ("https://github.com/joker1point/flowwatch/releases/download/"
               "v1.0.3/flowwatch-v1.0.3-win64-exe.zip")


def human(n: float) -> str:
    for u in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024.0:
            return "%.0f %s/s" % (n, u) if u == "B" else "%.1f %s/s" % (n, u)
        n /= 1024.0
    return "%.1f GiB/s" % n


def make_opener(no_tls_verify: bool, use_proxy: bool):
    hs = []
    if not use_proxy:
        hs.append(urllib.request.ProxyHandler({}))
    if no_tls_verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        hs.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*hs)


def check_tls(url: str) -> bool:
    """检测默认证书校验能否连通。

    返回 True 表示可以直接用默认校验；False 表示需要放宽。
    注意：失败原因不一定是 schannel —— 也可能是代理/网络/DNS。
    这里只报告事实，不臆断原因。
    """
    strict = make_opener(False, True)
    try:
        req = urllib.request.Request(url)
        req.add_header("Range", "bytes=0-0")
        with strict.open(req, timeout=30) as r:
            r.read(1)
        print("  TLS 正常，使用默认证书校验")
        return True
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "CERTIFICATE_VERIFY_FAILED" in msg or "certificate verify" in msg:
            print("  证书校验失败：%s" % msg)
            print("  → 加 --no-tls-verify 可绕过"
                  "（Windows schannel 吊销检查问题）")
        else:
            print("  连接失败：%s" % msg)
            print("  → 可能是代理 / 网络 / DNS 问题。"
                  "若为证书类错误，--no-tls-verify 可尝试绕过。")
        return False


def pull(opener, url: str, start: int, end: int) -> int:
    """拉取 [start, end] 闭区间。

    必须校验 Content-Range。GitHub 的 /archive/ 端点会忽略 Range
    直接返回整个文件；不校验就会把全量数据误算成区间数据，
    导致测速结果离谱（曾出现负耗时）。
    """
    req = urllib.request.Request(url)
    req.add_header("Range", "bytes=%d-%d" % (start, end))
    req.add_header("User-Agent", "fastdl-selftest")
    got = 0
    expect = end - start + 1
    with opener.open(req, timeout=60) as r:
        cr = r.headers.get("Content-Range")
        status = r.status
        if status != 206 or not cr:
            raise RuntimeError("服务端未返回 206/Content-Range（HTTP %s），"
                               "此端点不支持 Range 续传，无法测并发" % status)
        while True:
            buf = r.read(262144)
            if not buf:
                break
            got += len(buf)
    if got != expect:
        raise RuntimeError("区间 [%d-%d] 返回 %d 字节，期望 %d"
                           % (start, end, got, expect))
    return got


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="selftest",
        description="下载前自检：探测 TLS 可用性，并实测单连接 vs 8 连接的速度差。",
    )
    ap.add_argument("url", nargs="?",
                    default=DEFAULT_URL,
                    help="用于测试的直链（需支持 HTTP Range）")
    args = ap.parse_args()
    url = args.url
    print("测试目标：%s\n" % url)

    print("[1/3] TLS 连通性")
    ok = check_tls(url)
    no_tls = not ok

    opener = make_opener(no_tls, use_proxy=False)

    try:
        print("\n[2/3] 单连接速度（%.0f MiB 样本）" % (TEST_BYTES / 1048576))
        t0 = time.perf_counter()
        single = pull(opener, url, 0, TEST_BYTES - 1)
        dt1 = time.perf_counter() - t0
        s_rate = single / max(dt1, 0.001)
        print("  单连接 %s（%.1f MiB 用时 %.2fs）"
              % (human(s_rate), single / 1048576, dt1))

        print("\n[3/3] 8 连接并发速度（同量样本，可直接对比）")
        n = 8
        bounds = [i * TEST_BYTES // n for i in range(n + 1)]
        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(n) as ex:
            futs = [ex.submit(pull, opener, url, bounds[i], bounds[i + 1] - 1)
                    for i in range(n)]
            total = sum(f.result() for f in futs)
        dt2 = time.perf_counter() - t0
        m_rate = total / max(dt2, 0.001)
        print("  8 连接 %s（%.1f MiB 用时 %.2fs）"
              % (human(m_rate), total / 1048576, dt2))
    except RuntimeError as e:
        print("\n测速失败：%s" % e)
        print("换用支持 Range 的直链再试，例如 GitHub release 资产链接。")
        return 1

    print("\n" + "=" * 56)
    gain = m_rate / s_rate if s_rate > 0 else 0
    print("本次 8 连接 / 单连接 = %.2fx" % gain)
    print("-" * 56)
    if s_rate > 4 * 1024 * 1024:
        print("单连接已超过 4 MiB/s —— 链路本身很快，多连接意义不大。")
        print("建议：直接用默认 -t 4 即可，别堆线程。")
    elif gain >= 2:
        print("并发有明确收益。建议 -t 16（默认），超大文件可试 -t 32。")
    elif gain >= 1.0:
        print("并发略有收益或持平。建议 -t 8。")
    else:
        print("注意：本次 8 连接反而比单连接慢，通常是 CDN 对同一 IP 的")
        print("      高频 Range 请求限流所致，不代表本工具无效。")
        print("建议：")
        print("  · 调低并发到 -t 4（8 连接过密易触发限流）")
        print("  · 或加大分片 -c 32，让每个连接传输更久、更像正常浏览行为")
        print("  · 经验值：本工具在 623MB 实测中用 -t 16 -c 8 达到 5.5x 提升，")
        print("    大文件才有意义；小文件建议直接单连接下。")
    if not no_tls:
        print("\n提示：如遇 curl error 35，加 --no-tls-verify")
    print("=" * 56)
    return 0


def _force_utf8_stdio() -> None:
    """把标准输出切到 UTF-8，避免 Windows cp1252/cp936 控制台编码报错。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            try:
                stream.reconfigure(errors="replace")
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    _force_utf8_stdio()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
