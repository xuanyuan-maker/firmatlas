# 变更记录

## Unreleased

- 下载改用最终 HTTP 响应的 `Content-Length` 校验传输完整性，目录声明大小仅作为元数据。
- 下载器保存并统计原始响应字节；缺少或非法的 `Content-Length` 不再阻止正常归档。
- 删除下载用例中的声明大小容差和来源特殊大小校验。

## 0.1.2 - 2026-08-07

- 修复厂商以 KB/MB 换算、取整或有限小数位舍入声明固件大小时的 `size_mismatch` 误报。
- 普通来源默认允许最多 8 KiB 的声明大小误差；底层下载器默认仍执行精确大小校验。
- 官方 checksum 仍必须精确匹配，大小误差容差不会替代完整性校验。

## 0.1.1 - 2026-08-07

- 增加平台默认配置/数据目录及 `FIRMATLAS_CONFIG`、`FIRMATLAS_DATA_DIR` 优先级。
- 增加 Standalone/Managed 目录管理模式和 Catalog 配置校验。
- 增加纯净 SQLite Catalog 快照导出、manifest v1、确定性 gzip 和 SHA-256 校验。
- 增加 Catalog 状态、检查、同 lineage 更新、跨 lineage `--replace` 和下载记录迁移。
- 增加面向安全 Agent 的稳定 JSON 输出与错误代码。
- 增加服务器 systemd 采集、导出、资产验证和 GitHub Release 发布模板。
- 重写面向 PyPI 用户的发行版 README，并披露项目主要由 AI 辅助生成。

## 0.1.0

- 完成 MVP：来源采集、目录查询、按需下载、校验和本地归档。
