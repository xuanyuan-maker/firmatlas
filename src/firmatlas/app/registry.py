"""来源注册表：内置来源种子与适配器构造映射。

这是全项目唯一"认识所有适配器"的地方：
- SEED_SOURCES：`firmatlas init` 幂等写入 sources 表的内置来源；
- build_adapter()：CLI crawl 命令按 source_key 构造对应适配器。

新增来源（如阶段 5 的 tp-link-us）时在这两处各加一条即可。
"""

from __future__ import annotations

from pathlib import Path

from firmatlas.adapters.dahua_global.adapter import DahuaGlobalAdapter
from firmatlas.adapters.dlink_us.adapter import DlinkUsAdapter
from firmatlas.adapters.draytek_global.adapter import DraytekGlobalAdapter
from firmatlas.adapters.hikvision_global.adapter import HikvisionGlobalAdapter
from firmatlas.adapters.miwifi_cn.adapter import MiwifiCnAdapter
from firmatlas.adapters.tenda_global.adapter import TendaGlobalAdapter
from firmatlas.adapters.uniview_global.adapter import UniviewGlobalAdapter
from firmatlas.adapters.omada_global.adapter import OmadaGlobalAdapter
from firmatlas.adapters.tplink_cn.adapter import TplinkCnAdapter
from firmatlas.adapters.tplink_us.adapter import TplinkUsAdapter
from firmatlas.adapters.ruijie_cn.adapter import RuijieCnAdapter
from firmatlas.adapters.zyxel_global.adapter import ZyxelGlobalAdapter
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
            vendor_key="dahua",
            vendor_name="Dahua",
            source_key="dahua-global",
            name="Dahua 国际站固件下载中心",
            region_code="WW",
            locale="en",
            base_url="https://www.dahuasecurity.com/download-center/firmware",
            adapter_key="dahua_global",
            discovery_method=DiscoveryMethod.API,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
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
            vendor_key="xiaomi",
            vendor_name="Xiaomi",
            source_key="miwifi-cn",
            name="小米路由器 MiWiFi 下载页",
            region_code="CN",
            locale="zh-CN",
            base_url="https://www1.miwifi.com/miwifi_download.html",
            adapter_key="miwifi_cn",
            discovery_method=DiscoveryMethod.API,
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
            vendor_key="draytek",
            vendor_name="DrayTek",
            source_key="draytek-global",
            name="DrayTek 全球固件 FTP 服务器",
            region_code="WW",
            locale="en",
            base_url="https://fw.draytek.com.tw/",
            adapter_key="draytek_global",
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
        FirmwareSource(
            id=new_id(),
            vendor_key="zyxel",
            vendor_name="Zyxel",
            source_key="zyxel-global",
            name="Zyxel Global 固件下载中心",
            region_code="WW",
            locale="en",
            base_url="https://www.zyxel.com/global/en/",
            adapter_key="zyxel_global",
            discovery_method=DiscoveryMethod.HYBRID,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
        FirmwareSource(
            id=new_id(),
            vendor_key="tenda",
            vendor_name="Tenda",
            source_key="tenda-global",
            name="Tenda 全球站固件下载中心",
            region_code="WW",
            locale="en",
            base_url="https://www.tendacn.com/download",
            adapter_key="tenda_global",
            discovery_method=DiscoveryMethod.API,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
        FirmwareSource(
            id=new_id(),
            vendor_key="uniview",
            vendor_name="Uniview",
            source_key="uniview-global",
            name="宇视科技全球站固件下载中心",
            region_code="US",
            locale="en",
            base_url="https://global.uniview.com/us/Support/Download_Center/Firmware/",
            adapter_key="uniview_global",
            discovery_method=DiscoveryMethod.HTML,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
        FirmwareSource(
            id=new_id(),
            vendor_key="ruijie",
            vendor_name="Ruijie",
            source_key="ruijie-cn",
            name="锐捷中国站固件下载中心",
            region_code="CN",
            locale="zh-CN",
            base_url="https://www.ruijie.com.cn/fw/rj/",
            adapter_key="ruijie_cn",
            discovery_method=DiscoveryMethod.HYBRID,
            enabled=True,
            created_at=now,
            updated_at=now,
        ),
    ]


# source_key → 接收 HttpFetcher、返回适配器的构造函数
_ADAPTER_BUILDERS = {
    "dahua-global": DahuaGlobalAdapter,
    "dlink-us": DlinkUsAdapter,
    "draytek-global": DraytekGlobalAdapter,
    "hikvision-global": HikvisionGlobalAdapter,
    "miwifi-cn": MiwifiCnAdapter,
    "omada-global": OmadaGlobalAdapter,
    "tp-link-cn": TplinkCnAdapter,
    "tenda-global": TendaGlobalAdapter,
    "tp-link-us": TplinkUsAdapter,
    "uniview-global": UniviewGlobalAdapter,
    "ruijie-cn": RuijieCnAdapter,
    "zyxel-global": ZyxelGlobalAdapter,
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


def build_adapter(source_key: str, http: HttpFetcher, data_dir: Path | None = None) -> SourceAdapter:
    check_supported(source_key)
    cls = _ADAPTER_BUILDERS[source_key]
    # ruijie-cn 需要 data_dir 读取/保存认证 token
    if source_key == "ruijie-cn":
        return cls(http, data_dir)
    return cls(http)
