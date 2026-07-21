"""来源注册表：内置来源种子与适配器构造映射。

这是全项目唯一"认识所有适配器"的地方：
- SEED_SOURCES：`firmatlas init` 幂等写入 sources 表的内置来源；
- build_adapter()：CLI crawl 命令按 source_key 构造对应适配器。

新增来源（如阶段 5 的 tp-link-us）时在这两处各加一条即可。
"""

from __future__ import annotations

from firmatlas.adapters.dlink_us.adapter import DlinkUsAdapter
from firmatlas.adapters.hikvision_global.adapter import HikvisionGlobalAdapter
from firmatlas.adapters.omada_global.adapter import OmadaGlobalAdapter
from firmatlas.adapters.tplink_cn.adapter import TplinkCnAdapter
from firmatlas.adapters.tplink_us.adapter import TplinkUsAdapter
from firmatlas.app.crawl import SourceAdapter
from firmatlas.domain.errors import FirmAtlasError
from firmatlas.domain.ids import new_id
from firmatlas.domain.model import DiscoveryMethod, FirmwareSource
from firmatlas.domain.timeutil import utc_now
from firmatlas.infra.http_client import HttpFetcher


class UnsupportedSourceError(FirmAtlasError):
    """source_key 没有对应的适配器实现。"""


def seed_sources() -> list[FirmwareSource]:
    """内置来源列表（id 每次新生成；ensure_seed_sources 按 source_key 幂等跳过已有行）。"""
    now = utc_now()
    return [
        FirmwareSource(
            id=new_id(),
            vendor_key="tp-link",
            vendor_name="TP-Link",
            source_key="tp-link-cn",
            name="TP-Link 中国官网资料中心",
            region_code="CN",
            locale="zh-CN",
            base_url="https://resource.tp-link.com.cn/",
            adapter_key="tplink_cn",
            discovery_method=DiscoveryMethod.API,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
        FirmwareSource(
            id=new_id(),
            vendor_key="tp-link",
            vendor_name="TP-Link",
            source_key="tp-link-us",
            name="TP-Link 美国站下载中心",
            region_code="US",
            locale="en-US",
            base_url="https://www.tp-link.com/us/",
            adapter_key="tplink_us",
            discovery_method=DiscoveryMethod.HYBRID,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
        FirmwareSource(
            id=new_id(),
            vendor_key="hikvision",
            vendor_name="Hikvision",
            source_key="hikvision-global",
            name="Hikvision Global 固件下载站",
            region_code="WW",
            locale="en",
            base_url="https://www.hikvision.com/en/",
            adapter_key="hikvision_global",
            discovery_method=DiscoveryMethod.HTML,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
        FirmwareSource(
            id=new_id(),
            vendor_key="d-link",
            vendor_name="D-Link",
            source_key="dlink-us",
            name="D-Link 美国支持站资源目录",
            region_code="US",
            locale="en-US",
            base_url="https://support.dlink.com/",
            adapter_key="dlink_us",
            discovery_method=DiscoveryMethod.HTML,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
        FirmwareSource(
            id=new_id(),
            vendor_key="omada",
            vendor_name="Omada",
            source_key="omada-global",
            name="Omada Worldwide 固件下载中心",
            region_code="WW",
            locale="en",
            base_url="https://support.omadanetworks.com/en/",
            adapter_key="omada_global",
            discovery_method=DiscoveryMethod.API,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
    ]


# source_key → 接收 HttpFetcher、返回适配器的构造函数
_ADAPTER_BUILDERS = {
    "dlink-us": DlinkUsAdapter,
    "hikvision-global": HikvisionGlobalAdapter,
    "omada-global": OmadaGlobalAdapter,
    "tp-link-cn": TplinkCnAdapter,
    "tp-link-us": TplinkUsAdapter,
}

_LEGACY_TLS_SOURCE_KEYS = frozenset({"dlink-us"})


def supported_source_keys() -> list[str]:
    return sorted(_ADAPTER_BUILDERS)


def check_supported(source_key: str) -> None:
    """source_key 无对应适配器时抛 UnsupportedSourceError（供 CLI 在建 HTTP 客户端前校验）。"""
    if source_key not in _ADAPTER_BUILDERS:
        raise UnsupportedSourceError(
            f"不支持的来源 {source_key!r}，可用来源：{', '.join(supported_source_keys())}"
        )


def requires_legacy_tls(source_key: str) -> bool:
    """来源是否需要兼容旧 TLS 密码套件。"""
    check_supported(source_key)
    return source_key in _LEGACY_TLS_SOURCE_KEYS


def build_adapter(source_key: str, http: HttpFetcher) -> SourceAdapter:
    check_supported(source_key)
    return _ADAPTER_BUILDERS[source_key](http)
