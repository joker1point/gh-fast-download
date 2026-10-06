# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

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

[Unreleased]: https://github.com/joker1point/gh-fast-download/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/joker1point/gh-fast-download/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/joker1point/gh-fast-download/releases/tag/v1.0.0
