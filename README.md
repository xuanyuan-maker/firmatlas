# FirmAtlas

FirmAtlas 是一款面向个人漏洞研究者的 IoT 固件发现与按需获取命令行工具。它从厂商公开渠道采集固件元数据，建立可查询的本地 SQLite 目录；只有用户明确选择 Artifact 后才下载文件，并在校验成功后归档。

项目已完成 MVP，并在此基础上支持 TP-Link 中国站、TP-Link 美国站、海康威视国际站、
D-Link 美国站、Omada Worldwide、Zyxel Global、大华国际站、DrayTek 全球站、
小米路由器中国站、Tenda 全球站、宇视科技全球站和锐捷中国站十二个相互隔离的数据来源。
MVP 的全部 32 项验收标准已经通过。

## 核心能力

- 采集产品、硬件版本、固件发布和下载 Artifact 元数据。
- 识别普通路由器、Mesh 路由器、无线 AP、蜂窝 CPE 和摄像头。
- 按来源、地区、类型、型号、硬件版本、固件版本和状态组合查询。
- 重复采集保持幂等；完整采集后将下架记录标记为 `disappeared`，不硬删除。
- 按需流式下载，保存实际大小与 SHA-256；有官方校验和时强制比较。
- 下载地址失效时最多刷新一次，并保持 Artifact 身份不变。
- 校验成功后原子归档，数据库始终保存相对路径。

## 支持范围

| 来源 | 厂商 | 地区 | 发现方式 |
| --- | --- | --- | --- |
| `tp-link-cn` | TP-Link | CN | 资料中心公开 API |
| `tp-link-us` | TP-Link | US | 支持索引与固件下载页面 |
| `hikvision-global` | Hikvision | WW（Worldwide） | 国际站固件目录 HTML（摄像机切片） |
| `dlink-us` | D-Link | US | 美国支持站公开资源目录 |
| `omada-global` | Omada | WW（Worldwide） | 支持站公开产品树与固件 API |
| `zyxel-global` | Zyxel | WW（Worldwide） | Autocomplete API 与产品下载详情页 |
| `dahua-global` | Dahua | WW（Worldwide） | 国际站固件下载 API（摄像机） |
| `draytek-global` | DrayTek | WW（Worldwide） | FTP 固件服务器 Apache 目录列表 |
| `miwifi-cn` | Xiaomi | CN | MiWiFi 下载页与固件 API |
| `tenda-global` | Tenda | WW（Worldwide） | 全球站公开产品树与固件 API |
| `uniview-global` | Uniview | US | 全球站固件下载页 HTML（摄像机） |
| `ruijie-cn` | Ruijie | CN | 固件下载中心 API（需登录认证） |

标准产品类型为 `router`、`mesh_router`、`wireless_ap`、`cellular_cpe` 和 `camera`。交换机、无线网卡、独立控制器及其他范围外设备不会进入正式目录。

> **DrayTek 适配器已知限制**：fw.draytek.com.tw 服务器前面有 Cloudflare 防护，采集请求
> 间隔为 0.5s 以避免触发速率限制；部分老旧型号的 Firmware/ 子目录可能不存在（404），
> 导致这些产品无固件记录。部分型号（如 Vigor2866、Vigor2766、Vigor2763）同一版本下
> 存在多个固件变体（STD、MDM1-7），每个变体作为独立的 Artifact 记录。
>
> **Zyxel 适配器已知限制**：USG FLEX、ATP、VPN 等防火墙/安全网关系列的固件下载需
> myZyxel 登录（页面内下载链接指向 `portal.myzyxel.com`），适配器不绕过认证，这些型号
> 不会产出固件记录。Autocomplete API 使用"包含"匹配，多数前缀在两层递归后仍饱和，
> 当前 crawl 以 `partial` 状态完成（约 2-3 分钟），可能漏掉部分型号。实际采集结果
> 以 NWA/WAX/WBE 无线 AP 为主。
>
> **MiWiFi 适配器已知限制**：仅采集最新固件（API 端点为 `/upgrade/log/latest`），
> 不提供往期版本。产品列表从 index.json 提取，覆盖 50+ 款路由器，包括 BE10000 Pro
> 等新款 Wi-Fi 7 产品。小米官网不公布固件大小和校验和。
>
> **Uniview 适配器已知限制**：`uniview-global` 默认采集宇视全球站
> （global.uniview.com）的 Network Cameras、PTZ Cameras 和 Thermal Cameras
> 三个分类。该站仅列出 SE/SR 系列的 WP 型号，不包含 SS 系列。宇视中国站
> （cn.uniview.com）可能包含更多型号（包括 SS 系列），但其固件下载区
> （版本升级包 `Service/Service_Training/Download/Tools/`）需要合作伙伴级别
> 的 SSO 账号登录，普通注册账号返回 403 禁止访问，因此 cn.uniview.com 当前
> 不可用。固件以 ZIP 形式提供，多个产品型号可能共享同一固件文件；厂商不公布
> 固件校验和，适配器不实现 URL 刷新（CDN 直链不涉及短期失效）。

> **Ruijie 适配器已知限制**：`ruijie-cn` 需要锐捷官网登录后的 `GW_ACCESS_TOKEN`
> Cookie 才能调用固件详情与下载地址 API。Token 有效期约 8 小时，过期需重新获取。
> 执行 `firmatlas crawl ruijie-cn` 时若未配置 token，CLI 会交互式引导输入；也可通过
> 环境变量 `RUIJIE_TOKEN` 或 `firmatlas auth ruijie-cn --save "<token>"` 提前配置。
> 适配器采集锐捷官网固件下载中心的 4 个路由器/无线/网关分类页，不涉及摄像头产品；
> 版本详情采用最多 4 路并发，来源级请求启动间隔为 0.25 秒。下载地址是短期有效的 OSS
> 签名 URL，因此 crawl 阶段只保存 `pending:{file_id}` 占位符，用户执行 download 时才
> 实时解析。
>
> 2026-07-24 的一次完整写库实测覆盖 310 个产品入口，产出 264 个产品、1,304 个发布和
> 1,679 个 Artifact，耗时约 9 分 3 秒。实际耗时会随厂商站点响应、网络和产品数量变化。

## 环境要求

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Linux、macOS 或其他能够运行 Python 与 SQLite 的环境

## 快速开始

安装运行及开发依赖：

```bash
uv sync --dev
```

初始化本地数据目录：

```bash
uv run firmatlas --data-dir data init
```

锐捷来源需要先保存登录 token。直接把 token 写在命令行中可能进入 shell 历史；在 zsh
中可以隐藏输入并在保存后立即清除临时变量：

```zsh
read -rs "RUIJIE_TOKEN_INPUT?请输入锐捷 token: "; echo
uv run firmatlas --data-dir data auth ruijie-cn --save "$RUIJIE_TOKEN_INPUT"
unset RUIJIE_TOKEN_INPUT
uv run firmatlas --data-dir data auth ruijie-cn --check
```

查看来源并采集元数据：

```bash
uv run firmatlas --data-dir data sources
uv run firmatlas --data-dir data crawl tp-link-cn
uv run firmatlas --data-dir data crawl tp-link-us
uv run firmatlas --data-dir data crawl hikvision-global
uv run firmatlas --data-dir data crawl dlink-us
uv run firmatlas --data-dir data crawl omada-global
uv run firmatlas --data-dir data crawl zyxel-global
uv run firmatlas --data-dir data crawl dahua-global
uv run firmatlas --data-dir data crawl draytek-global
uv run firmatlas --data-dir data crawl miwifi-cn
uv run firmatlas --data-dir data crawl tenda-global
uv run firmatlas --data-dir data crawl uniview-global
uv run firmatlas --data-dir data crawl ruijie-cn
```

如果当前终端的代理设置导致国际站访问异常，可以仅对本次采集临时取消代理环境变量：

```bash
env -u all_proxy -u http_proxy -u https_proxy \
  -u ALL_PROXY -u HTTP_PROXY -u HTTPS_PROXY \
  uv run firmatlas --data-dir data crawl hikvision-global
```

这也适用于代理变量指向已经停止的本地代理服务时；例如将命令末尾来源替换为
`ruijie-cn`。取消代理只影响当前命令，不修改终端或系统的全局代理配置。

浏览和筛选固件目录：

```bash
uv run firmatlas --data-dir data list --type router --format table
uv run firmatlas --data-dir data list --source tp-link-cn --model XDR --format json
uv run firmatlas --data-dir data list --source hikvision-global --type camera
uv run firmatlas --data-dir data list --source omada-global --type wireless_ap
uv run firmatlas --data-dir data list --source zyxel-global --type wireless_ap
uv run firmatlas --data-dir data list --source dahua-global --type camera
uv run firmatlas --data-dir data list --source draytek-global --type router
uv run firmatlas --data-dir data list --source draytek-global --type wireless_ap
uv run firmatlas --data-dir data list --source miwifi-cn --type router
uv run firmatlas --data-dir data list --source tenda-global --type router
uv run firmatlas --data-dir data list --source tenda-global --type wireless_ap
uv run firmatlas --data-dir data list --source tenda-global --type camera
uv run firmatlas --data-dir data list --source uniview-global --type camera
uv run firmatlas --data-dir data list --source ruijie-cn --type router
uv run firmatlas --data-dir data list --source ruijie-cn --type wireless_ap
uv run firmatlas --data-dir data show <release-id>
```

选择固件下载并查看历史。`download` 接受 `list` 输出的发布 ID，也接受 `show` 输出的
Artifact ID；两者都可以使用无歧义的 ID 前缀：

```bash
uv run firmatlas --data-dir data download <release-id-or-artifact-id>
uv run firmatlas --data-dir data downloads
```

锐捷 Artifact 在 crawl 后可能显示 `pending:{file_id}`，这是预期状态。download 用例会先
获取当前有效的临时地址，再流式下载、比较官方 MD5（若提供）、计算 SHA-256 并原子归档。

所有全局选项必须放在子命令之前。使用以下命令查看完整帮助：

```bash
uv run firmatlas --help
uv run firmatlas list --help
```

## 数据与下载流程

```text
厂商公开站点
    ↓ 仅采集元数据
本地 SQLite 目录
    ↓ 用户 list/show 筛选
选择 Artifact
    ↓ 流式下载到 data/tmp/downloads
大小与校验和验证
    ↓ 原子移动
data/firmware/厂商/地区/型号/硬件版本/固件版本/
```

采集命令默认不会下载固件。不要提交运行时 `data/`、真实固件、Cookie、密钥或未脱敏的厂商响应。

## 配置

默认值可由 TOML 配置文件和显式 CLI 参数覆盖，优先级为“默认值 → 配置文件 → CLI”。查看最终生效配置：

```bash
uv run firmatlas --config firmatlas.toml --data-dir data config
```

配置文件可调整 HTTP 请求/连接超时、重试次数、退避基数和下载超时。请求间隔及并发上限
由来源策略和实现控制，不提供无限并发配置。

## 项目结构

```text
src/firmatlas/
├── domain/      # 领域模型、标识符和错误
├── app/         # 采集、查询、下载与恢复用例
├── adapters/    # TP-Link、Hikvision、D-Link、DrayTek、Omada、Zyxel、Dahua、小米、Tenda、Uniview 与 Ruijie 来源适配器
├── infra/       # SQLite、HTTP、Repository、下载与归档
└── cli/         # Click 命令行入口
tests/           # pytest 测试与脱敏 fixture
```

## 开发与验证

```bash
uv run pytest
uv run ruff check .
uv build
```

网络相关自动化测试使用脱敏 fixture、MockTransport 或本地回环服务，不依赖实时厂商网站。
