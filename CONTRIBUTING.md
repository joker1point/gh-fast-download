# 贡献指南

感谢你愿意花时间改进这个工具。

## 开发

零依赖，克隆即用：

```bash
git clone https://github.com/joker1point/gh-fast-download.git
cd gh-fast-download
python tests/test_offline.py -v
```

所有测试都是离线的，不需要网络。CI 会在
Linux / Windows / macOS × Python 3.8 / 3.12 六种组合下运行。

## 代码风格

- 保持**零第三方依赖**，只用标准库 —— 这是工具的核心卖点
- Python 3.8+ 兼容（不要用 `match`、`X | Y` 类型语法）
- 注释写「为什么」，不写「做了什么」
- 网络异常统一重试，不要静默吞掉

## 提交前请确认

- [ ] `python tests/test_offline.py` 全部通过
- [ ] 新增行为有对应测试
- [ ] 网络相关改动在**至少两种网络条件**下验证过
      （直连/代理、Range 支持/不支持）
- [ ] README 中的参数表与实际 `--help` 一致

## 需要特别注意的两个坑

这两个是本项目实测踩过的，改动时别踩回去：

1. **Range 请求必须校验 `Content-Range`。**
   GitHub 的 `/archive/` 端点会忽略 Range 直接返回全量文件。
   不校验就会把全量数据误算成区间数据，测速结果离谱（会出现负耗时）。

2. **收尾清理必须非致命。**
   删除分片可能触发环境层面的批量删除钩子而被拦截。
   清理失败只能打印警告，**绝不能影响退出码** ——
   否则会出现「下载成功但任务报failed」的假象。

## 报告 Bug

请附上：

- 操作系统与 Python 版本
- 完整命令行（**token 请打码**）
- 完整输出
- `python selftest.py` 的结果

## 许可

提交即表示你同意你的贡献以 MIT 许可发布。
