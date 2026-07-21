# FirmAtlas

FirmAtlas 是一款面向个人漏洞研究者的 IoT 固件发现与按需获取命令行工具。它从厂商公开渠道采集固件元数据，建立可查询的本地 SQLite 目录；只有用户明确选择 Artifact 后才下载文件，并在校验成功后归档。

项目已完成 MVP，并在此基础上支持 TP-Link 中国站、TP-Link 美国站、海康威视国际站
和 D-Link 美国站四个相互隔离的数据来源。MVP 的全部 32 项验收标准已经通过。

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

标准产品类型为 `router`、`mesh_router`、`wireless_ap`、`cellular_cpe` 和 `camera`。交换机、无线网卡、独立控制器及其他范围外设备不会进入正式目录。

### 海康威视国际站

`hikvision-global` 当前只采集国际站明确归类的网络摄像机、PTZ 摄像机、热成像摄像机、
Turbo HD 摄像机和 HiLook 摄像机。每个 `Applied to` 型号作为独立产品；同一版本的不同
地域固件作为同一发布下的多个 Artifact。中国站与国际站是独立来源，不会跨地区合并。

2026-07-20 使用真实国际站完成采集与下载验证：一次完整采集写入 5292 个 Product、
5338 个 Release 和 5343 个 Artifact，采集错误为 0；随后完成固件流式下载、SHA-256
计算和原子归档。厂商目录持续变化，实际数量以每次采集结果为准。

### D-Link 美国站

`dlink-us` 从 [D-Link 官方资源目录](https://support.dlink.com/resource/PRODUCTS/) 发现固件，
并采用型号白名单，只采集以下目标设备：DIR、DGL、COVR、R、M、GO-RT、
EBR、DI、DSL 系列路由器，DWR 蜂窝 CPE，DCS 摄像机，DBA 企业无线 AP，以及
DSR、DFL、DBG、DBR VPN/公网网关。DWC、DWS、DNH 无线控制器不采集；需要逐型号
判断的 DAP、DWL 以及交换机、NVR、NAS、网卡和智能家居设备也暂不纳入。

D-Link 服务器使用较旧的 TLS 配置。FirmAtlas 只对 `dlink-us` 降低 OpenSSL 安全级别，
仍验证服务器证书和主机名，不影响其他来源。HTTP 客户端支持环境中的 HTTP、HTTPS 和
SOCKS 代理；例如 `ALL_PROXY=socks5://localhost:10808` 可以直接用于采集。

2026-07-21 使用真实目录完成完整采集：发现 179 个 Product、851 个 Release 和 851 个
Artifact，跳过 1 项、错误 0、问题记录 3 条。厂商目录持续变化，实际数量以每次采集
结果为准。

### Cisco RV 系列调研（尚未接入）

Cisco RV 系列目前只是已验证的数据源候选，尚未注册为 FirmAtlas 来源，因此不存在
`firmatlas crawl cisco` 命令。本次调研环境直接访问
[Cisco Software Download](https://software.cisco.com/download/home) 页面会被 Akamai 拒绝，
但 [Cisco RV 支持页](https://www.cisco.com/c/en/us/support/routers/small-business-rv-series-routers/series.html#~tab-downloads)
的 Downloads 标签会调用公开、匿名的软件目录 API，可以避开对动态下载中心 HTML 的依赖。

公开目录当前列出 15 个型号：RV042、RV042G、RV110W、RV132W、RV134W、RV160、
RV160W、RV215W、RV260、RV260P、RV260W、RV340、RV340W、RV345 和 RV345P。
其中 13 个型号的摘要接口返回最新固件；RV110W 和 RV215W 当前没有返回固件条目。

| 型号 | 当前摘要接口返回的最新固件 |
| --- | --- |
| RV042、RV042G | 4.2.3.14 |
| RV132W | 1.0.1.15 |
| RV134W | 1.0.1.21 |
| RV160、RV160W、RV260、RV260P、RV260W | 1.0.01.10 |
| RV340、RV340W、RV345、RV345P | 1.0.03.29 |

RV 系列的父级 MDF ID 是 `282413304`。页面从公开的 `downloadconfig.json` 读取客户端
配置，再依次调用 `catalog/v1/products`、`summary/v1/loadsoftwareinfo`、
`image/v1/details` 和 `download/v1/image`。未来实现不应复制某次浏览器会话中的临时
签名 URL，也不应记录 Cookie。

已验证的数据流为：

```text
RV 系列 MDF ID
    ↓ 公开产品目录 API
型号与各自 MDF ID
    ↓ 软件摘要 API
最新版本、文件名、Image GUID 与匿名访问级别
    ↓ Image 详情 API
发布日期、大小、MD5、SHA-512 与发布文档
    ↓ 用户明确下载时请求下载 API
有时效的匿名签名 URL → 固件文件
```

以 RV340 为例，公开接口返回固件 `1.0.03.29`、文件大小 74,913,162 字节以及 MD5、
SHA-512；通过临时签名 URL 下载固件的请求返回 HTTP 200。签名 URL 会过期，未来若接入
适配器，应使用 Image GUID 作为稳定来源标识，并仅在用户明确下载时获取临时地址。

当前公开摘要接口只覆盖最新版本；“All Releases”仍会进入受 Akamai 保护的下载中心，
历史版本枚举接口尚未确认。因此 Cisco RV 暂不列入上方“支持范围”，也不能执行完整的
历史版本采集与消失对账。下载行为还必须遵守页面展示的 Cisco General Terms。

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

查看来源并采集元数据：

```bash
uv run firmatlas --data-dir data sources
uv run firmatlas --data-dir data crawl tp-link-cn
uv run firmatlas --data-dir data crawl tp-link-us
uv run firmatlas --data-dir data crawl hikvision-global
uv run firmatlas --data-dir data crawl dlink-us
```

如果当前终端的代理设置导致国际站访问异常，可以仅对本次采集临时取消代理环境变量：

```bash
env -u all_proxy -u http_proxy -u https_proxy \
  uv run firmatlas --data-dir data crawl hikvision-global
```

浏览和筛选固件目录：

```bash
uv run firmatlas --data-dir data list --type router --format table
uv run firmatlas --data-dir data list --source tp-link-cn --model XDR --format json
uv run firmatlas --data-dir data list --source hikvision-global --type camera
uv run firmatlas --data-dir data show <release-id>
```

选择固件下载并查看历史。`download` 接受 `list` 输出的发布 ID，也接受 `show` 输出的
Artifact ID；两者都可以使用无歧义的 ID 前缀：

```bash
uv run firmatlas --data-dir data download <release-id-or-artifact-id>
uv run firmatlas --data-dir data downloads
```

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

超时、重试次数、请求并发和下载并发均有有限默认值，不能配置为无限值。

## 项目结构

```text
src/firmatlas/
├── domain/      # 领域模型、标识符和错误
├── app/         # 采集、查询、下载与恢复用例
├── adapters/    # TP-Link CN/US、Hikvision Global 与 D-Link US 来源适配器
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
