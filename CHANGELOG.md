# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

## [1.2.0] - 2026-10-06

### 新增

- **`--limit-mb N`**：只下载前 N MiB。用途有两个：
  - **试水**：文件很大时先拉几 MB 看看速度，再决定要不要下完
  - **可复现的测速对比**：让两种下载方式传输**完全相同的字节数**，
    对比才公平（否则快的那个可能只是"下得少"）
  - 实现上直接把「文件大小」当成 N MiB，分片逻辑自然只覆盖 `[0, N MiB)`；
    产物是截断文件，因此**会跳过 sha256 校验**并明确提示
- **`demo/compare_download.py`**：实机演示脚本，
  一条命令跑完「新用户流程」并与普通单连接下载做对照，打印对比表
- **`demo/compare_download.py --preflight`**：上台前预检，
  用 4 MiB 样本测候选素材，告诉你今天这条链路并发有没有收益
- **`demo/RUNBOOK.md`**：现场演示台词、观众提问的答法、翻车兜底方案

### 关于演示脚本的三个坑（都踩过，都修了）

1. **样本必须能切出多片，否则演示出的「没有加速」是假的。**
   fastdl 默认分片 8 MiB；若样本只有 4 MiB，只会切出 **1 片 = 单连接**，
   结果必然是「没有加速」——这是假的结论。
   所以演示脚本默认用 `-c 1`（1 MiB 分片），4 MiB 样本 → 4 片并发。
2. **两种方式必须传输相同字节数。** 这正是 `--limit-mb` 的由来。
3. **演示脚本下载的是「已发布」脚本，新功能未发版时缺选项。**
   现在会先探测发布版是否支持所需选项，不支持就明确提示并退回仓库版本。

### 一个我下错的结论（记录在案）

初版对比中 `cli/cli` 素材跑出 0.90x，而 `flowwatch` 跑出 2.31x。
我据此断言「cli/cli 的 CDN 对同 IP 做了聚合限速」——**这个结论是错的**。

换到有效样本重测后：`cli/cli` 2.06x、`flowwatch` 2.45x，**两个都有效**。
那个 0.90x 是**网络波动**，不是素材特性。

教训：**两个数据点不足以支撑因果结论**。为此加了 `--preflight`，
因为"今天能不能演"本来就不是靠选素材能保证的，只能现场实测。

另外预检自身也踩了坑 1：用 1 MiB 样本测出 0.70x 的假象。
现已强制样本 ≥ 4 MiB（`ensure_parallel_sample()`），并把该不变量写进测试。

## [1.1.0] - 2026-10-06

本次起因：以**新用户视角**从零走一遍流程，发现三道拦路门槛。

### 新增

- **`--list-assets`**：列出某个 release 的全部资产及大小，不下载。
  新用户最常卡在"`--asset` 到底填什么"，现在一条命令就能查，
  不用去网页上一个个抄
- **公开仓库不再需要 `gh` CLI 或 token**（重要修复，见下）

### 修复

- **公开仓库下载被误报"需要认证"** —— 这是新用户第一眼就会撞上的门槛。
  此前 `gh_api()` 在 gh CLI 不可用且未传 token 时，直接抛
  `RuntimeError("无法调用 gh CLI，请安装 gh 并登录，或传入 --token")`。
  但下载**公开** release 根本不需要认证，这句话纯属误导。
  - 现改为：无 token 时走**匿名 GitHub API 请求**（限速 60 次/小时，
    取一次 release 元数据绰绰有余）
  - 仅当匿名请求返回 404/403 时，才提示可能需要认证
  - 匿名请求**绝不携带** `Authorization` 头（有测试钉住）
- **粘错链接不再抛裸 traceback**：把 release 页面或仓库主页地址
  当作 URL 传入时，此前会一路走到 `probe_size`，而 GitHub 对 HTML 返回
  `Content-Range: bytes 0-0/*`，`int('*')` 抛
  `ValueError: invalid literal for int() with base 10: '*'`
  - 根因修复：`_parse_total()` 正确处理 `*`（总长未知）与畸形输入
  - 体验修复：新增 `github_url_hint()`，识别 release 页 / 仓库页 /
    代码浏览链接，直接告诉用户该改成哪条命令
  - CLI 兜底捕获 `ValueError`，解析类问题不再以堆栈形式呈现
- 详情未知时的提示更具体：区分"链接指向网页"与"服务端不支持 Range"，
  并给出"右键复制真实下载地址"的操作指引

### 变更

- 多资产 / 资产名写错时，可选列表改为**逐行竖排**（此前挤在一行难读）

### 测试

- 43 个离线测试（较 v1.0.1 新增 15 个），新增覆盖：
  - `TestAnonymousApiAccess`：无 token 时确实发出匿名请求、
    匿名请求不带凭据、有 token 才带凭据、gh CLI 可用时优先走 CLI
  - `TestGithubUrlHint`：直链放行、各类网页链接拦截并给出正确命令
  - `TestContentRangeParsing`：`bytes 0-0/*` 不崩溃、
    `bytes */N`（RFC 7233）正确解析、畸形输入返回 None
  - `TestListAssetsCli`：参数校验

## [1.0.1] - 2026-10-06

### 安全

- **修复：`gh_api()` 无条件关闭 TLS 证书校验**（由独立安全审查发现）
  - 此前该函数在调用 `api.github.com` 时硬编码了
    `check_hostname = False` / `verify_mode = CERT_NONE`，
    同时又在请求头中发送 `Authorization: token ...`
  - 影响比"用户主动选择不安全模式"更严重：**任何**走该 urllib 回退路径
    的调用都会静默关闭校验，用户在不知情的情况下可能被中间人窃取凭据
  - 现改为：`no_tls_verify` 参数默认 `False`（正常校验证书），
    放宽校验只能由调用方显式请求并逐层透传
- **新增安全护栏**：当「关闭 TLS 校验」与「携带凭据」同时成立时，
  打印醒目告警并给出替代方案（`gh auth login` / SSH）
- `--token` 与 `--no-tls-verify` 的帮助文本写明风险
- 新增 `TestCredentialSafety` 6 项测试钉住上述契约，防止回归

> 若你曾**同时**使用 `--token` 和 `--no-tls-verify`，建议轮换 token。

### 修复

- 网络错误不再抛出原始 traceback，改为友好提示：
  - `URLError` → 提示检查网络连通性
  - `HTTPError` → 401/403 提示认证与权限方向，404 提示检查仓库与资产名
  - `OSError` → 磁盘空间 / 权限 / 路径问题

### 变更

- `selftest.py` 默认测试目标改为本项目自己的 release 资产，
  不再指向其他仓库
- `selftest.py` 新增样本量下限（8 MiB）：样本不足时**拒绝给出并发结论**
  并提示换用更大的文件。小样本耗时几乎全落在 TLS 握手与 RTT 上，
  据此得出的结论会失真——与其给误导性答案，不如直说测不了
- `test_offline.py` 改进模块定位：从所在目录向上查找**最近**的
  `fastdl.py`，兼容「与 `fastdl.py` 同级」和「放在 `tests/` 子目录」两种布局
  - 修复一个隐患：原先无条件把父目录加入 `sys.path`，
    若父目录存在同名的无关 `fastdl.py` 会导入到错误模块
- release 附件新增 `test_offline.py`，便于使用者独立复验

## [1.0.0] - 2026-10-06

### 新增

- 首个版本：并行分片下载器
  - 多线程分片并发，突破单连接限速（实测最高 7.6 倍提升）
  - 断点续传：中断后重跑同一条命令即可，已下载分片不浪费
  - sha256 校验：`--gh-release` 模式自动取 GitHub 官方 digest
  - 零依赖，仅标准库，Python 3.8+，跨平台
- `--gh-release` / `--tag` / `--asset`：直接从 GitHub release 下载
- `selftest.py`：下载前自检，实测单连接 vs 8 连接并建议线程数

### 已规避的坑

- Windows schannel 证书吊销检查失败（`curl error 35`）→ `--no-tls-verify`
- 系统代理拖慢下载 → 默认绕过（`--use-proxy` 可开启）
- Windows 控制台中文输出崩溃 → 导入时强制 UTF-8
- 收尾清理拖垮主流程 → 清理失败只警告，不影响退出码
- `Range` 未校验导致测速失真 → 强制校验 `Content-Range`

[Unreleased]: https://github.com/joker1point/gh-fast-download/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/joker1point/gh-fast-download/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/joker1point/gh-fast-download/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/joker1point/gh-fast-download/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/joker1point/gh-fast-download/releases/tag/v1.0.0
