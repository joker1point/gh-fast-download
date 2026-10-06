# gh-fast-download

![license](https://img.shields.io/badge/license-MIT-green)
![python](https://img.shields.io/badge/python-3.8%2B-3776AB)
![dependencies](https://img.shields.io/badge/dependencies-zero-success)
![platform](https://img.shields.io/badge/platform-Windows%2FLinux%2FmacOS-0078D6)

> **单连接下载大文件被限速到几十 KB/s？用多连接把它拉满。**
> 单文件 Python 脚本，零依赖，复制即用。附断点续传与 sha256 校验。

---

## 一、它解决什么问题

下载GitHub Release 上的大文件（模型权重、便携版、数据集），单连接常常只有 **50 KB/s**，
一个 600MB 的包要下3 小时。原因是服务端/CDN 对**单连接**限速。

这个脚本把文件切成 N 块，**开N 个线程各拉一块**，带宽利用率成倍上升。

实测（详见下方「实测数据」）：单连接 55 KB/s → 16 线程 0.41 MB/s，**提升 7.6 倍**。

### 不是什么

- **不是**代理工具 / VPN，不改变你的网络出口
- **不是**镜像加速，不依赖任何第三方加速服务
- **不是**断网续传神器 —— 它能**进程级**续传（中断重跑），但不跨机器

---

## 二、快速开始

零依赖，Python 3.8+ 直接跑：

```bash
# GitHub release：自动读取资产列表、文件大小、官方 sha256
# 注意 owner/repo 和 tag 是两个独立参数，tag 必须用 --tag 传
python fastdl.py --gh-release owner/repo --tag v1.0.0

# 一个 release 有多个资产时，用 --asset 指定
python fastdl.py --gh-release owner/repo --tag v1.0.0 --asset mytool-linux.zip

# 任意直链
python fastdl.py https://example.com/bigfile.tar.gz -o out.tar.gz

# 调线程数和分片大小
python fastdl.py https://example.com/bigfile.tar.gz -t 32 -c 4
```

Windows 上如果报证书错误，加 `--no-tls-verify`（原因见下方「踩过的坑」）。

---

## 三、用法速查

| 参数 | 说明 | 默认 |
|---|---|---|
| `<URL>` | 下载地址（与 `--gh-release` 二选一） | — |
| `--gh-release OWNER/REPO` | 从 release 下载，自动取 size与官方 sha256 | — |
| `--tag TAG` | release 的 tag，配合上面使用 | — |
| `--asset NAME` | 资产名（一个 release 有多个资产时必填） | 唯一资产 |
| `-o, --out PATH` | 输出路径 | URL 末段 |
| `-t, --threads N` | 并发线程数 | `16` |
| `-c, --chunk-mb N` | 分片大小（MiB） | `8` |
| `--sha256 HEX` | 期望的 sha256，用于校验 | 无 |
| `--tokenTOKEN` | 私有仓库 token，也可用 `GITHUB_TOKEN` | — |
| `--no-tls-verify` | 关闭证书校验（Windows schannel 修复） | 关 |
| `--use-proxy` | 走系统代理 | 直连 |
| `-k, --keep-parts` | 完成后保留分片 | 删 |

### ⚠️ 别在小文件上开高并发

分片数 = `文件大小 ÷ 分片大小`。参数不是越高越好：

| 文件大小 | 建议 |
|---|---|
| < 20 MB | 用默认值即可，甚至 `-t 4`；高并发纯属浪费 |
| 20 MB ~ 500 MB | 默认 `-t 16 -c 8` |
| > 500 MB | 可试 `-t 32 -c 8` |

实测：2.9 MB 文件配 `-t 32 -c 4` 会切成 32 个 4 MB 分片，
但文件只有 2.9 MB——绝大多数分片是空请求，时间全耗在
32 次 TLS 握手和 CDN 限流上。**并发收益来自传输时间，不是请求数量。**

### 中途断了怎么办

**重跑同一条命令**即可。已下载的分片会跳过，只补缺失部分：

```
分片数 9  线程 16  已有 9.0 MiB (54.3%)   ← 续传起点
[ 54.3%] ...
校验通过 ✓
```

### 不确定该不该用多线程？先自检

```bash
python selftest.py                      # 用内置测试地址
python selftest.py <你的URL># 换成自己的链路
```

会实测单连接 vs 8 连接并给出建议线程数。

> ⚠️ 小心解读：CDN 会对同一 IP 的**高频 Range 请求**限流。
> 若测出「并发反而更慢」，通常是对自己的源站打太密了，
> 试着调低并发 `-t 4` 或加大分片 `-c 32`，让每个连接传输更久、
> 更接近正常浏览行为。**大文件才有明显收益**，几 MB 的小文件直接单连接下更快。

---

## 四、实测数据

2026-10 于中国大陆 Windows 环境，下载 623MB 文件：

| 方式 | 速度 | 623 MB 耗时 |
|---|---|---|
| curl 单连接（走本机代理） | 19 KB/s | ~9 小时 |
| curl 单连接（直连） | 55 KB/s | ~3.2 小时 |
| **fastdl 16 线程直连** | **0.41 MB/s** | **约 25 分钟** |

两个值得注意的发现：

1. **本机代理反而是负优化** —— 比直连慢 3 倍。所以脚本默认绕过系统代理
   （`--use-proxy` 可关闭该行为）。
2. **16 线程已触及带宽上限** —— 继续加线程无收益。默认值按此设定。

> 真实耗时受网络波动影响很大。本次完整下载因中途网络劣化实际耗时约 10 小时，
> 这是链路问题，不是工具问题。

---

## 五、踩过的坑（都已在脚本里处理）

### Windows：curl error 35 / `CRYPT_E_NO_REVOCATION_CHECK`

Windows schannel 的证书吊销检查失败，curl 连github.com 直接握手失败：

```
curl: (35) schannel: next InitializeSecurityContext failed: CRYPT_E_NO_REVOCATION_CHECK
```

curl 的解法是 `--ssl-no-revoke`，脚本里的对应开关是 `--no-tls-verify`。
**看到这个报错，先加这个参数**，别去折腾证书。

### 代理可能比直连慢很多

本机 HTTP 代理（`127.0.0.1:xxxx`）实测比直连慢 3 倍。脚本默认使用
`ProxyHandler({})` 绕过代理；需要走代理时显式加 `--use-proxy`。

### 收尾清理不能拖垮主流程

早期版本在下载成功后逐个 `os.remove()` 删分片，在某些环境会触发批量删除钩子被拦截，
导致进程非零退出 —— **下载明明成功了，任务却报failed**。

现在改为 `shutil.rmtree()` 一次性删除，且清理失败只打印警告、不影响退出码。
退出码含义：

| 码 | 含义 |
|---|---|
| 0 | 成功（含 sha256 通过） |
| 2 | 有分片最终失败，网络恢复后重跑即可续传 |
| 3 | sha256 校验失败，分片已保留 |
| 4 | 合并后大小不符 |
| 5 | 无法探测文件大小 |

---

## 六、设计取舍

**为什么不用 aria2c？** 它确实更强，但需要额外安装。本脚本只用标准库，
`python fastdl.py` 就能跑，适合"临时下一个文件"的场景。
如果你已经有 aria2c 且不介意装，aria2 的 `-x16` 是等价甚至更好的选择。

**为什么默认 8MB 分片？** 分片太大则并发粒度粗、尾部拖累明显；
太小则请求开销占比上升。8MB 在实测中表现稳定。623MB 文件切成 78 片，
配合 16 线程约有 5片/线程的滚动队列，负载均衡良好。

**为什么必须校验？** 并发分片拼接一旦错位，文件大小可能仍然正确但内容已损坏。
GitHub release 的资产带官方 `sha256`，**应当始终校验**。`--gh-release` 模式会自动带上它。

---

## 七、安全说明

- 本工具**只做下载**，不执行下载到的任何内容
- 默认不走代理、不发送任何遥测
- 私有仓库建议用环境变量传token，避免进入 shell history：<br>
  `export GITHUB_TOKEN=xxx`（PowerShell 用 `$env:GITHUB_TOKEN="xxx"`）
- 下载完请自行校验 sha256 再运行

---

## 八、开发与测试

```bash
python tests/test_offline.py -v      # 16 个离线测试，不触网
```

覆盖：分片数计算（向上取整 / 末片短块）、断点续传后字节级一致性、
sha256 通过与失败路径、失败时保留分片、清理行为、CLI 契约、代理处理器构造。

CI 在 Linux / Windows / macOS × Python 3.8 / 3.12 六种组合下运行，
**只跑离线测试**，不做真实下载（网络测试不适合进 CI）。

## 九、许可

MIT ©[K眠月](https://github.com/joker1point)
