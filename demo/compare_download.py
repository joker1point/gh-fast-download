#!/usr/bin/env python3
"""实机演示：新用户流程 vs 普通下载方式。

一条命令跑完，最后打印对照表：

    python demo/compare_download.py
    python demo/compare_download.py --limit-mb 6      # 演示更久一点
    python demo/compare_download.py --repo owner/repo --tag v1.0.0 --asset x.zip

设计要点（都是为了让演示站得住，别改坏）：

  · **两种方式传输完全相同的字节数**（都用 --limit-mb 卡住），
    否则快的那个可能只是"下得少"，对比不成立
  · 分片必须 ≥ 2 片才能体现并发，所以默认 -c 1 而不是默认的 -c 8。
    8MiB 分片配 4MiB 样本会只切出 1 片，等于单连接，演示不出加速
  · 计时用 perf_counter（单调钟），不受系统校时影响
  · 结束后核对两个产物的 sha256，证明并发分片没有拼错数据

⚠️ 演示前建议先跑一次确认耗时。网络快时两者差距会很小——这本身也是
   一个诚实的结论（并发收益取决于链路是否被限速），脚本会如实解读。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_FASTDL = os.path.join(ROOT, "fastdl.py")

DEFAULT_REPO = "cli/cli"
DEFAULT_TAG = "v2.102.0"
DEFAULT_ASSET = "gh_2.102.0_windows_amd64.zip"   # 14.8 MB

# ⚠️ 为什么必须先跑 --preflight：**同一素材的提速波动极大**。
#
# 2026-10-06 本机实测（4 MiB 样本、-c 1、8 线程、相隔数小时）：
#   cli/cli  windows zip : 0.90x（第一次）→ 2.06x（第二次）
#   flowwatch win64-exe  : 2.31x         → 2.45x
#
# 注意 cli/cli 那两行差了 2.3 倍。我一度以为这是「该仓库 CDN 做了 IP 级
# 聚合限速」的素材特性，但换到有效样本重测后就翻案了 —— 那是**网络波动**，
# 不是素材特性。教训：两个数据点不足以支撑因果结论。
#
# 结论：素材名气大小与提速无关，**能不能演出效果主要取决于当时的链路状况**。
# 所以这里保留名气大的第三方仓库做默认（演示更可信），
# 而把「先测一把」这件事交给 --preflight。
PREFLIGHT_REPOS = [
    ("cli/cli", "v2.102.0", "gh_2.102.0_windows_amd64.zip"),
    ("joker1point/flowwatch", "v1.0.3", "flowwatch-v1.0.3-win64-exe.zip"),
]

STEPS = 5


# ---------------------------------------------------------------- 小工具

def human(n: float) -> str:
    for u in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024.0:
            return "%.1f %s" % (n, u) if u != "B" else "%d B" % n
        n /= 1024.0
    return "%.1f TiB" % n


def rate(n: float) -> str:
    return human(n) + "/s"


def banner(n: int, title: str) -> None:
    print("")
    print("=" * 70)
    print("[%d/%d] %s" % (n, STEPS, title))
    print("=" * 70)


def make_opener(no_tls_verify: bool):
    hs = [urllib.request.ProxyHandler({})]   # 与本工具一致：默认绕过代理
    if no_tls_verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        hs.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*hs)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 对照 A

def single_connection_download(opener, url: str, out: str, nbytes: int) -> float:
    """普通下载：一个连接顺序读完。

    浏览器「另存为」、curl -L、wget 都是这个语义，这就是对照组的含义。
    """
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "fastdl-demo/baseline")
    req.add_header("Range", "bytes=0-%d" % (nbytes - 1))

    done = 0
    t0 = time.perf_counter()
    with opener.open(req, timeout=120) as r, open(out, "wb") as f:
        while done < nbytes:
            buf = r.read(262144)
            if not buf:
                break
            f.write(buf)
            done += len(buf)
    return time.perf_counter() - t0


# ---------------------------------------------------------------- 对照 B

def fastdl_download(fastdl: str, args: argparse.Namespace, out: str,
                    repo: str | None = None, tag: str | None = None,
                    asset: str | None = None, limit_mb: int | None = None,
                    chunk_mb: int | None = None, quiet: bool = False) -> float:
    """本工具下载。直接调用真实 CLI，演示的就是用户实际会跑的命令。

    repo/tag/asset/limit_mb/chunk_mb 可覆盖，供预检逐一测试候选素材。
    """
    cmd = [
        sys.executable, fastdl,
        "--gh-release", repo or args.repo,
        "--tag", tag or args.tag,
        "--asset", asset or args.asset,
        "-o", out, "-t", str(args.threads),
        "-c", str(chunk_mb if chunk_mb is not None else args.chunk_mb),
        "--limit-mb", str(limit_mb if limit_mb is not None else args.limit_mb),
    ]
    if args.no_tls_verify:
        cmd.append("--no-tls-verify")

    t0 = time.perf_counter()
    sink = subprocess.DEVNULL if quiet else None
    proc = subprocess.run(cmd, stdout=sink, stderr=sink)
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        print("  ⚠ fastdl 退出码 %d" % proc.returncode, file=sys.stderr)
    return elapsed


# ---------------------------------------------------------------- 预检

# 预检样本至少要能切成这么多片，否则测的其实是单连接
PREFLIGHT_MIN_PARTS = 4


def ensure_parallel_sample(sample_mb: int, chunk_mb: int) -> int:
    """把样本抬高到能切出 PREFLIGHT_MIN_PARTS 片的最小 MiB 数。

    这个函数存在的唯一理由：**样本切不出多片时，测出来的「并发无收益」是假的。**
    本项目已经在这上面栽过两次 ——
      · 首次写演示脚本：4 MiB 样本配默认 8 MiB 分片 → 1 片 → 显示 0.90x
      · 首次写预检：1 MiB 样本配 1 MiB 分片     → 1 片 → 显示 0.70x（同期
        4 MiB 样本实测明明是 2.31x）
    """
    if chunk_mb <= 0:
        raise ValueError("chunk_mb 必须为正")
    return max(sample_mb, chunk_mb * PREFLIGHT_MIN_PARTS)


def preflight(args: argparse.Namespace) -> int:
    """上台前先跑：用很小样本测几个候选素材，告诉你哪个能演出加速。

    为什么需要它：不同 asset 主机对并发 Range 的策略不同。实测 cli/cli
    的单连接反而比 flowwatch 快，但并发对它没有收益（0.90x）——
    不做预检，很可能站在台上才发现演示效果是「并发更慢」。
    """
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    sys.path.insert(0, ROOT)
    import fastdl as _m  # noqa: E402

    # 样本下限：分片粒度是整数 MiB，最小 1 MiB，
    # 要让并发成立（≥PREFLIGHT_MIN_PARTS 片）就必须抬到足够大。
    # 1 MiB 样本只会切出 1 片 = 单连接，测出来必然是"并发没收益"的假象。
    chunk_mb = 1
    sample_mb = ensure_parallel_sample(args.limit_mb, chunk_mb)
    sample = sample_mb * 1024 * 1024

    workdir = os.path.join(ROOT, "_demo")
    os.makedirs(workdir, exist_ok=True)
    opener = make_opener(args.no_tls_verify)

    print("=" * 70)
    print("预检：候选素材的并发收益")
    print("  样本 %s，分片 %d MiB → 每个素材切成 %d 片"
          % (human(sample), chunk_mb, sample_mb // chunk_mb))
    print("  （样本必须能切出多片，否则测的是单连接，必然显示「无收益」）")
    print("=" * 70)
    print("")

    results = []
    for repo, tag, asset in PREFLIGHT_REPOS:
        print("→ %s @ %s" % (repo, tag))
        try:
            data = _m.gh_api("repos/%s/releases/tags/%s" % (repo, tag), None)
            a = next((x for x in data["assets"] if x["name"] == asset), None)
            if a is None:
                print("   找不到资产 %s，跳过" % asset)
                continue
            url = a["browser_download_url"]
        except Exception as e:  # noqa: BLE001
            print("   查询失败：%s，跳过" % e)
            continue

        base_out = os.path.join(workdir, "pf_base.bin")
        fast_out = os.path.join(workdir, "pf_fast.bin")
        for p in (base_out, fast_out, fast_out + ".parts"):
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)

        try:
            tb = single_connection_download(opener, url, base_out, sample)
            tf = fastdl_download(args.fastdl, args, fast_out, repo=repo, tag=tag,
                                 asset=asset, limit_mb=sample_mb,
                                 chunk_mb=chunk_mb, quiet=True)
        except Exception as e:  # noqa: BLE001
            print("   测量失败：%s，跳过" % e)
            continue

        rb = sample / max(tb, 0.001)
        rf = sample / max(tf, 0.001)
        gain = rf / rb if rb else 0
        results.append((gain, repo, tag, asset))
        print("   单连接 %8s   并发 %8s   提速 %.2fx"
              % (rate(rb), rate(rf), gain))
        print("")

    if not results:
        print("没测出可用结果，请检查网络后重试。")
        return 1

    results.sort(reverse=True)
    print("=" * 70)
    print("结论")
    print("=" * 70)
    for gain, repo, tag, asset in results:
        verdict = ("✓ 推荐" if gain >= 2 else
                   "△ 勉强" if gain >= 1.3 else
                   "✗ 会翻车（并发无收益）")
        print("  %-38s %6.2fx  %s" % (asset[:38], gain, verdict))
    print("")

    best_gain, best_repo, best_tag, best_asset = results[0]
    if best_gain >= 2:
        print("  → 用这个上台：")
        print("    python demo/compare_download.py --no-tls-verify \\")
        print("      --repo %s --tag %s --asset %s --limit-mb %d"
              % (best_repo, best_tag, best_asset, sample_mb))
    elif best_gain >= 1.3:
        print("  → 收益一般但能看。建议加大样本（--limit-mb 8）再测，")
        print("    或换单连接更慢的素材 —— 越慢越有戏。")
    else:
        print("  → 当前网络下并发普遍无收益，通常意味着：")
        print("    · 这条链路单连接已不被限速（此刻这工具确实帮不上忙），或")
        print("    · 本地网络拥堵 / CDN 在做 IP 级限流")
        print("    建议换个时段再演示。")
        print("")
        print("    ⚠ 不要硬演。如实说明「并发收益取决于链路是否被限速」，")
        print("      本身就是这个项目价值主张的一部分。")
    print("=" * 70)
    return 0


# ---------------------------------------------------------------- 主流程

def supports_option(script: str, option: str) -> bool:
    """检查某个 fastdl.py 是否支持指定选项。

    演示时务必用正式发布的脚本（新用户拿到的就是它）。但新功能若还没发版，
    脚本会缺选项——此时应给出明确提示并退回仓库里的版本，
    而不是让它抛一个 'unrecognized arguments' 的裸错误。
    """
    try:
        out = subprocess.run([sys.executable, script, "--help"],
                             capture_output=True, text=True, timeout=30)
        return option in (out.stdout or "")
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    # 演示输出要与子进程输出交织，行缓冲避免顺序错乱
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="新用户流程 vs 普通下载，实机对比演示")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--asset", default=DEFAULT_ASSET)
    ap.add_argument("--limit-mb", type=int, default=4,
                    help="两种方式各传输多少 MiB（默认 4，演示建议 4~8）")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--chunk-mb", type=int, default=1,
                    help="分片大小 MiB。默认 1 以保证小样本也能切出多片")
    ap.add_argument("--preflight", action="store_true",
                    help="只做「预检」：用很小样本测几个候选素材，"
                         "告诉你哪个能演出加速效果。上台前先跑这个")
    ap.add_argument("--fastdl", default=DEFAULT_FASTDL)
    ap.add_argument("--no-tls-verify", action="store_true",
                    help="Windows schannel 吊销检查失败时需要")
    args = ap.parse_args()

    if args.preflight:
        return preflight(args)

    workdir = os.path.join(ROOT, "_demo")
    os.makedirs(workdir, exist_ok=True)
    local_fastdl = os.path.join(workdir, "fastdl.py")
    base_out = os.path.join(workdir, "baseline.bin")
    fast_out = os.path.join(workdir, "fastdl.bin")

    # 清掉上次残留：否则「文件是否产出」的判断会被旧文件骗过，
    # 分片目录也可能让本次续传到旧内容上。
    for p in (base_out, fast_out, fast_out + ".parts",
              os.path.join(workdir, "pf_base.bin"),
              os.path.join(workdir, "pf_fast.bin"),
              os.path.join(workdir, "pf_fast.bin.parts")):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)

    nbytes = args.limit_mb * 1024 * 1024
    parts = (nbytes + args.chunk_mb * 1048576 - 1) // (args.chunk_mb * 1048576)

    # ---------------- 1. 环境
    banner(1, "起点：一个「新用户」的机器")
    print("  Python        : %s" % sys.version.split()[0])
    gh = subprocess.run(["gh", "--version"], capture_output=True, text=True)
    print("  gh CLI        : %s" % ("已安装" if gh.returncode == 0 else "未安装"))
    print("  GITHUB_TOKEN  : %s" % ("已设置" if os.environ.get("GITHUB_TOKEN")
                                      else "未设置"))
    print("")
    print("  → 下载公开仓库这两样都不需要。下面全程不碰它们。")

    opener = make_opener(args.no_tls_verify)

    # ---------------- 2. 拿到脚本
    banner(2, "新用户第一步：拿到脚本（不用 clone）")
    url_script = ("https://github.com/joker1point/gh-fast-download/releases/"
                  "latest/download/fastdl.py")
    print("  $ curl -LO %s" % url_script)
    try:
        req = urllib.request.Request(url_script)
        req.add_header("User-Agent", "fastdl-demo")
        with opener.open(req, timeout=60) as r, open(local_fastdl, "wb") as f:
            f.write(r.read())
        print("  → 得到 fastdl.py（%s）" % human(os.path.getsize(local_fastdl)))
    except Exception as e:  # noqa: BLE001
        print("  下载脚本失败：%s" % e)
        print("  → 改用仓库里那份 fastdl.py 继续演示")
        local_fastdl = args.fastdl

    # 发布版若还不支持本演示需要的选项，明确说明并退回仓库版本。
    if not supports_option(local_fastdl, "--limit-mb"):
        ver = subprocess.run([sys.executable, local_fastdl, "--version"],
                             capture_output=True, text=True).stdout.strip()
        print("")
        print("  ⚠ 注意：发布版（%s）尚不支持 --limit-mb，"
              "本演示需要它来保证两种方式传输相同字节数。" % (ver or "未知版本"))
        print("    这不是工具的问题，是「新功能还没发版」。")
        print("    → 本次改用仓库里的 fastdl.py 继续（演示新用户路径时，")
        print("      步骤 2/3 的行为与发布版一致）。")
        local_fastdl = args.fastdl

    # ---------------- 3. 列出资产
    banner(3, "新用户第二步：不知道资产名？列出来")
    print("  $ python fastdl.py --gh-release %s --tag %s --list-assets"
          % (args.repo, args.tag))
    print("")
    subprocess.run([sys.executable, local_fastdl, "--gh-release", args.repo,
                    "--tag", args.tag, "--list-assets"])

    # ---------------- 解析真实下载地址
    sys.path.insert(0, ROOT)
    import fastdl as _m  # noqa: E402

    data = _m.gh_api("repos/%s/releases/tags/%s" % (args.repo, args.tag), None)
    asset = next((a for a in data["assets"] if a["name"] == args.asset), None)
    if asset is None:
        print("\n找不到资产 %r，请用 --asset 指定。" % args.asset, file=sys.stderr)
        return 2
    url = asset["browser_download_url"]
    full_size = int(asset["size"])

    # ---------------- 4. 对照 A
    banner(4, "对照 A：普通下载（单连接 —— 浏览器另存为 / curl -L 同理）")
    print("  $ curl -L -o baseline.bin '<直链>'")
    print("")
    print("  两者都只传输前 %s（文件共 %s）" % (human(nbytes), human(full_size)))
    print("  —— 传输字节数相同，对比才公平")
    print("")
    base_t = single_connection_download(opener, url, base_out, nbytes)
    print("  完成：%s，用时 %.1f 秒" % (human(os.path.getsize(base_out)), base_t))

    # ---------------- 5. 对照 B
    banner(5, "对照 B：fastdl（%d 线程 / %d MiB 分片 → %d 片并发）"
           % (args.threads, args.chunk_mb, parts))
    print("  $ python fastdl.py --gh-release %s --tag %s --asset %s \\"
          % (args.repo, args.tag, args.asset))
    print("        -t %d -c %d --limit-mb %d" % (args.threads, args.chunk_mb,
                                                 args.limit_mb))
    print("")
    fast_t = fastdl_download(local_fastdl, args, fast_out)

    # ---------------- 结果
    if not os.path.exists(base_out):
        print("\n  ✗ 对照 A 没有产出文件，无法继续。", file=sys.stderr)
        return 1
    if not os.path.exists(fast_out):
        print("\n  ✗ fastdl 没有产出文件（原因见上方输出），无法对比。",
              file=sys.stderr)
        return 1

    base_size = os.path.getsize(base_out)
    fast_size = os.path.getsize(fast_out)
    base_r = base_size / max(base_t, 0.001)
    fast_r = fast_size / max(fast_t, 0.001)
    speedup = (fast_r / base_r) if base_r else 0

    print("")
    print("=" * 70)
    print("结果")
    print("=" * 70)
    print("  %-28s %10s %14s" % ("方式", "耗时", "速度"))
    print("  " + "-" * 56)
    print("  %-28s %9.1fs %14s" % ("普通下载（单连接）", base_t, rate(base_r)))
    print("  %-28s %9.1fs %14s" % ("fastdl（%d 线程并发）" % args.threads,
                                   fast_t, rate(fast_r)))
    print("  " + "-" * 56)
    print("  提速：%.2fx" % speedup)
    print("")
    print("  正确性核对")
    print("    · 两者字节数 ：%s vs %s  %s"
          % (human(base_size), human(fast_size),
             "✓" if base_size == fast_size else "✗"))
    same = sha256_of(base_out) == sha256_of(fast_out)
    print("    · 内容 sha256 ：%s" % ("一致 ✓（并发分片没拼错）" if same
                                      else "不一致 ✗"))

    if not asset.get("digest"):
        print("    · 官方 digest ：该资产无官方值，跳过")

    print("")
    if speedup >= 3:
        print("  解读：并发收益显著。文件越大，省下的绝对时间越多。")
    elif speedup >= 1.3:
        print("  解读：有收益。适合「反正要下」的场景。")
    else:
        print("  解读：本次收益不明显。")
        print("        说明这条链路单连接已经不慢，或 CDN 对高频 Range 限流。")
        print("        这也是真实结论：并发收益取决于链路是否被限速。")
        print("")
        print("  → 演示前建议先跑 `--preflight` 挑素材，避免上台才发现是 0.9x。")
    print("=" * 70)
    print("演示产物：%s（可直接删）" % workdir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        sys.exit(130)
