# FirmAtlas 服务器部署

这些文件把规范服务器部署成同一 FirmAtlas wheel 的 `Standalone` 角色：服务器只执行
`init`、逐来源 `crawl`、`catalog export`，不执行 `download`。GitHub Release 发布目标默认是
独立的 `xuanyuan-maker/firmatlas-catalog` 仓库。

## 安装

1. 创建受保护用户和持久化目录：`firmatlas`、`/var/lib/firmatlas`。
2. 安装 FirmAtlas wheel、`gh` 和 `jq`。
3. 将 `server.env.example` 复制为 `/etc/firmatlas/server.env`，设置 `root:firmatlas` 和
   `0640` 权限；Token 只放在该文件或 systemd credential 中。
4. 将 `firmatlas-cycle.sh`、`verify-release-assets.sh` 和
   `publish-catalog-release.sh` 安装到 `/usr/local/libexec/` 并设为不可写可执行文件。
5. 安装 service/timer，执行 `systemctl enable --now firmatlas-catalog.timer`。

发布前脚本会拒绝缺失资产、非 gzip、非零 download_records、校验和错误或损坏压缩包。
默认任一来源失败就不发布；只有显式设置 `FIRMATLAS_ALLOW_PARTIAL=true` 才允许发布，manifest
中的来源最近状态仍会如实保留。

成功发布后，`FIRMATLAS_EXPORT_RETENTION`（默认 5）控制服务器上可重建的导出缓存数量；脚本不
清理规范数据库或 `data/backups/`。service 的标准输出和错误输出进入 journald，按服务器统一的
journald 保留策略轮换；可用 `journalctl -u firmatlas-catalog.service` 审计周期日志。

## 恢复

- `data/firmatlas.db` 是规范数据库，必须定期离线备份，并与 `catalog-lineage-id` 一起保存。
- `data/backups/` 是客户端更新备份，不替代服务器离线备份。
- 数据库损坏时，先停止 timer，从最近的规范数据库备份恢复，再运行
  `firmatlas --data-dir /var/lib/firmatlas init` 检查版本。
- 如果规范数据库无法证明属于原 lineage，创建新的 lineage，并从完整 crawl 后发布新的
  Catalog；不要根据产品名称猜测旧 Artifact 身份。
- 发布失败可重新运行同一周期；旧 Release 不覆盖，客户端仍可使用旧快照回滚。
- 备份轮换必须保留最近一次可恢复的规范数据库备份；数据库备份的清理应由独立的备份策略执行，
  不由采集脚本递归删除。

不要把 `data/`、真实固件、Token、Cookie、日志或导出文件复制进代码仓库。
