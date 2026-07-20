# FirmAtlas MVP 验收记录

## 验收结论

- 验收日期：2026-07-20
- 代码基线：`7c2c15d`
- 运行环境：Python 3.12.13、SQLite schema `user_version=1`
- 结果：README 0x10 的 AC-01～AC-32 全部通过，MVP 验收完成

验收以脱敏 fixture、临时 SQLite 数据目录和本地回环 HTTP 服务为主，不依赖实时厂商网站。TP-Link CN/US 的真实站点采集与下载验证记录另见 `PLAN.md`。

## 验收证据

| 编号 | 状态 | 主要证据 |
| --- | --- | --- |
| AC-01 | 通过 | `uv sync --dev --locked` 成功；`uv run python --version` 为 3.12.13；CLI 版本为 0.1.0。 |
| AC-02 | 通过 | 全局帮助及 `config/crawl/download/downloads/init/list/runs/show/sources --help` 均以状态码 0 返回。 |
| AC-03 | 通过 | 临时目录连续执行两次 `init` 均成功；`PRAGMA user_version=1`，共有7张业务表；见 `test_init.py`、`test_schema.py`。 |
| AC-04 | 通过 | `sources` 同时列出 `tp-link-cn` 与 `tp-link-us`；两套端到端测试分别完成 crawl→query。 |
| AC-05 | 通过 | crawl 数据流仅保存元数据 Candidate，不调用 Downloader；下载只能由独立 `download` 用例触发。 |
| AC-06 | 通过 | CN/US 分类契约覆盖 `router`、`mesh_router`、`wireless_ap`、`cellular_cpe`、`camera` 五类。 |
| AC-07 | 通过 | 分类测试排除交换机、控制器、无线网卡、网关、NVR、配件及未知分类。 |
| AC-08 | 通过 | `test_skipped_candidates_recorded_in_issues` 验证无法分类记录以 `skipped_*` issue 保存并跳过。 |
| AC-09 | 通过 | CN 标题与 US 页面解析器分别生成独立 HardwareRevisionCandidate；适配器及端到端测试验证完整产品树。 |
| AC-10 | 通过 | 每个硬件版本下的 FirmwareReleaseCandidate 和 Artifact 全部进入目录；端到端查询数等于采集发布数。 |
| AC-11 | 通过 | `test_cn_us_isolated_no_cross_region` 及数据库唯一约束验证来源、产品、硬件版本不会跨层合并。 |
| AC-12 | 通过 | `test_ids.py`、CN `test_source_keys_are_stable`、US `test_source_key_contract` 覆盖实体身份生成规则。 |
| AC-13 | 通过 | Repository、crawl 及 CN/US 端到端重采测试均验证不产生重复记录。 |
| AC-14 | 通过 | 新 `source_key` 仅新增对应目录实体；crawl 用例不创建下载任务，首次发现时间在重采时保持不变。 |
| AC-15 | 通过 | `test_complete_crawl_marks_unseen_as_disappeared` 验证完整采集后仅标记消失，不删除记录。 |
| AC-16 | 通过 | 不完整声明、缺少完成事件、持久化失败等测试均验证不执行消失对账。 |
| AC-17 | 通过 | crawl 与 Repository 生命周期测试验证重新出现后恢复为 `active`。 |
| AC-18 | 通过 | SQLite 文件在独立 CLI 进程间重复打开；端到端 CLI 测试在重开数据库后仍可 list/show。 |
| AC-19 | 通过 | `app/ports.py` 定义 Repository/UoW 端口，SQLite 实现在 infra；`domain/`、`app/` 无 SQLAlchemy/sqlite3 导入。 |
| AC-20 | 通过 | adapters 仅产出事件和 Candidate；静态检查确认没有 Repository、SQLAlchemy、Downloader 或文件存储依赖。 |
| AC-21 | 通过 | `test_list_combined_filters` 覆盖来源、地区、类型、型号、硬件版本等组合筛选。 |
| AC-22 | 通过 | CLI 查询测试覆盖同一目录的 table 与 JSON 输出。 |
| AC-23 | 通过 | `test_cli_list_json_schema_and_no_ansi` 验证 `schema_version` 且 stdout 无 ANSI。 |
| AC-24 | 通过 | CLI `--format` 仅接受 `table/json`；手动传入 `csv` 返回参数错误。 |
| AC-25 | 通过 | CLI 下载测试覆盖 Artifact ID、ID 前缀、Release ID 及多 Artifact 选择提示。 |
| AC-26 | 通过 | 成功下载测试验证 SHA-256、实际大小、相对归档路径及数据库记录全部保存。 |
| AC-27 | 通过 | SHA-256/MD5 匹配测试通过；checksum mismatch 测试验证失败文件不会进入正式归档。 |
| AC-28 | 通过 | 无官方校验和时完成归档并记录 `verification_status=not_available`。 |
| AC-29 | 通过 | 404 刷新测试验证最多刷新一次、只重试一次且 Artifact `source_key` 不变。 |
| AC-30 | 通过 | Repository 部分唯一索引及下载用例均拒绝同一 Artifact 的第二个活动任务。 |
| AC-31 | 通过 | CN/US 网络相关测试使用 `tests/fixtures/`、MockTransport、假适配器或本地回环服务，不访问真实厂商站点。 |
| AC-32 | 通过 | 初始化与打开数据库测试验证未知/不匹配 `user_version` 被拒绝，原数据保持不变。 |

## 发布级检查

```text
uv sync --dev --locked       通过
uv run pytest -q             281 passed
uv build                     sdist + wheel 构建成功
uv run ruff check .          通过
```

下载器的真实 HTTP 行为测试需要在 `127.0.0.1` 创建临时服务；受限沙箱内无法创建套接字，在允许本地回环后76项下载与查询专项测试全部通过。

## 非阻塞维护项

- `ruff format --check .` 报告26个历史文件尚未统一格式；不影响本次32项功能验收，后续应作为独立机械整理提交。
- TP-Link US 全量280型号当前串行采集超过10分钟；功能正确，但可在 MVP 后增加受配置约束的型号级并发。
- TP-Link CN 少量2017～2018旧标题仍会被明确记录为解析失败并跳过；如需回补历史固件，可独立扩展标题解析器。
