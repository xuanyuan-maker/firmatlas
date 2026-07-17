"""tp-link-cn 适配器（接口设计 §4）。

从 TP-Link 中国站资料中心搜索 API 发现固件，产出 DiscoveryEvent 流。

## 数据流

搜索 API（POST /api/v1/material-center/search）按 productClassIds +
softwareType=UPGRADE_SOFT 返回平铺的固件记录，每条记录的 title 字段编码了：
  型号 + 硬件版本 + 日期 + 固件版本

适配器的工作：
1. 对每个粗筛品类分页拉取全部记录
2. parse_title() 解析 title 提取型号/硬件版本/固件版本
3. classify() 判定产品 family 和 product_type
4. 按 产品→硬件版本→固件版本 三级分组建树
5. 逐产品产出 DiscoveredProduct；非目标记录产出 SkippedCandidate
6. 最后产出 DiscoveryCompleted
"""

from __future__ import annotations

from firmatlas.adapters.events import (
    AdapterIssueSummary,
    DiscoveredProduct,
    DiscoveryCompleted,
    SkippedCandidate,
    SkipReason,
)
from firmatlas.adapters.tplink_cn.classification import (
    Classification,
    candidate_product_class_ids,
    classify,
)
from firmatlas.adapters.tplink_cn.title_parser import parse_title
from firmatlas.domain.candidates import (
    FirmwareArtifactCandidate,
    FirmwareReleaseCandidate,
    HardwareRevisionCandidate,
    ProductCandidate,
)
from firmatlas.domain.model import ArtifactType
from firmatlas.infra.http_client import HttpFetcher

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_SEARCH_URL = "https://resource.tp-link.com.cn/api/v1/material-center/search"
_PAGE_SIZE = 100
_RESOURCE_CENTER_URL = "https://resource.tp-link.com.cn/"

# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


class TplinkCnAdapter:
    """TP-Link 中国站适配器。

    构造时注入 HttpFetcher，不得自建 HTTP 客户端。
    source_key 固定为 "tp-link-cn"。
    """

    source_key = "tp-link-cn"

    def __init__(self, http: HttpFetcher) -> None:
        self._http = http

    # -- discover -------------------------------------------------------

    async def discover(self):
        """异步生成器，逐事件产出发现结果。"""
        issues: list[AdapterIssueSummary] = []
        skipped: list[SkippedCandidate] = []
        # 三级分组：model → hw_version → fw_version → [artifacts]
        product_groups: dict[str, _ProductTree] = {}

        class_ids = candidate_product_class_ids()

        for cid in class_ids:
            page_index = 1
            total = 1  # 先设为非零值进入循环

            while (page_index - 1) * _PAGE_SIZE < total:
                try:
                    fetched = await self._http.post_json(
                        _SEARCH_URL,
                        body={
                            "crossFilter": [
                                {
                                    "filterClassIds": [],
                                    "category": "SOFTWARE",
                                    "documentTypes": [],
                                    "softwareTypes": ["UPGRADE_SOFT"],
                                }
                            ],
                            "formats": ["zip"],
                            "productClassIds": [cid],
                            "pageIndex": page_index,
                            "pageSize": _PAGE_SIZE,
                            "sortOrder": "DEFAULT",
                            "orderDirection": "DESC",
                            "isMaterial": True,
                            "keyword": "",
                            "tagIds": None,
                        },
                        headers={"Referer": _RESOURCE_CENTER_URL},
                    )
                except Exception as exc:
                    issues.append(
                        AdapterIssueSummary(
                            code="api_error",
                            detail=f"品类 {cid} 第 {page_index} 页请求失败: {exc}",
                        )
                    )
                    break  # 跳过当前品类，继续下一个

                if fetched.status_code != 200:
                    issues.append(
                        AdapterIssueSummary(
                            code="api_status",
                            detail=f"品类 {cid} 第 {page_index} 页返回 {fetched.status_code}",
                        )
                    )
                    break

                result = fetched.data.get("result")
                if not result:
                    break

                total = result.get("total", 0)
                collection = result.get("collection") or []

                for record in collection:
                    self._process_record(
                        record, cid, product_groups, skipped, issues
                    )

                page_index += 1

        # --- 产出分组后的产品树 ---
        for _model_key, tree in product_groups.items():
            product = self._build_product_candidate(tree)
            yield DiscoveredProduct(product=product)

        # --- 产出跳过的记录 ---
        for s in skipped:
            yield s

        # --- 完成 ---
        yield DiscoveryCompleted(
            is_complete=True,
            incomplete_reason=None,
            issues=tuple(issues),
        )

    # -- 单条记录处理 ---------------------------------------------------

    def _process_record(
        self,
        record: dict,
        class_id: str,
        product_groups: dict[str, _ProductTree],
        skipped: list[SkippedCandidate],
        issues: list[AdapterIssueSummary],
    ) -> None:
        """解析单条搜索记录，分类并挂到 product_groups 树上。

        不可分类 → 追加 SkippedCandidate 到 skipped 列表。
        """
        api_id = str(record.get("id", ""))
        title = record.get("title", "")
        url = record.get("url", "")
        doc_size_kb = record.get("docSize")

        if not title or not url:
            skipped.append(
                SkippedCandidate(
                    stage="artifact",
                    reason_code=SkipReason.MISSING_IDENTITY,
                    detail=f"缺少 title 或 url (id={api_id})",
                    source_url=None,
                    raw_hint=api_id,
                )
            )
            return

        # 1. 解析标题
        parsed = parse_title(title)
        if parsed is None:
            skipped.append(
                SkippedCandidate(
                    stage="artifact",
                    reason_code=SkipReason.PARSE_FAILED,
                    detail=f"无法解析标题: {title}",
                    source_url=url,
                    raw_hint=api_id,
                )
            )
            return

        # 2. 分类
        classification = classify(class_id, parsed.model_raw, parsed.model_raw)
        if classification is None:
            skipped.append(
                SkippedCandidate(
                    stage="artifact",
                    reason_code=SkipReason.UNMAPPED_TYPE,
                    detail=(
                        f"型号 {parsed.model_raw}（品类 {class_id}）"
                        f"不在采集范围"
                    ),
                    source_url=url,
                    raw_hint=api_id,
                )
            )
            return

        # 3. 计算规范化值
        model_normalized = _normalize_model(parsed.model_raw)
        hw_normalized = _normalize_hw_version(parsed.hardware_version_raw)

        # 4. 挂到 product_groups 树上
        tree = product_groups.setdefault(
            model_normalized,
            _ProductTree(
                model_raw=parsed.model_raw,
                model_normalized=model_normalized,
                classification=classification,
            ),
        )
        tree.add_artifact(
            hw_raw=parsed.hardware_version_raw,
            hw_normalized=hw_normalized,
            fw_version=parsed.firmware_version,
            release_date=parsed.release_date,
            title=title,
            api_id=api_id,
            download_url=url,
            doc_size_kb=doc_size_kb,
        )

    # -- 构建 ProductCandidate -----------------------------------------

    def _build_product_candidate(self, tree: _ProductTree) -> ProductCandidate:
        """将一棵 _ProductTree 转换为 ProductCandidate。"""
        cls = tree.classification
        hw_candidates: list[HardwareRevisionCandidate] = []

        for hw_norm, hw_node in sorted(tree.hardware_revisions.items()):
            release_candidates: list[FirmwareReleaseCandidate] = []

            for fw_ver, fw_node in sorted(hw_node.releases.items()):
                # 取该发布下的第一条 artifact 的信息作为发布级元数据
                first = fw_node.artifacts[0]
                release_source_key = _make_release_source_key(
                    tree.model_normalized, hw_norm, fw_ver
                )

                artifact_candidates = tuple(
                    FirmwareArtifactCandidate(
                        source_key=art.api_id,
                        artifact_type=ArtifactType.FIRMWARE,
                        original_filename=_extract_filename(art.download_url),
                        download_url=art.download_url,
                        url_expires_at=None,  # tp-link-cn URL 无显式过期
                        advertised_size=_kb_to_bytes(art.doc_size_kb),
                        media_type="application/zip",
                        official_checksum=None,  # API 不提供官方校验和
                    )
                    for art in fw_node.artifacts
                )

                release_candidates.append(
                    FirmwareReleaseCandidate(
                        source_key=release_source_key,
                        version_raw=fw_ver,
                        version_normalized=fw_ver,
                        release_date=first.release_date,
                        title=first.title,
                        release_notes=None,
                        release_notes_url=None,
                        source_url=first.download_url,
                        artifacts=artifact_candidates,
                    )
                )

            hw_source_key = _make_hw_source_key(tree.model_normalized, hw_norm)
            hw_candidates.append(
                HardwareRevisionCandidate(
                    source_key=hw_source_key,
                    raw_revision=f"V{hw_node.raw}",
                    normalized_revision=hw_norm,
                    revision_explicit=True,
                    source_url=None,
                    releases=tuple(release_candidates),
                )
            )

        return ProductCandidate(
            source_key=tree.model_normalized,
            display_name=tree.model_raw,
            model_raw=tree.model_raw,
            model_normalized=tree.model_normalized,
            series=None,  # tp-link-cn 无系列信息
            product_family=cls.family,
            product_type=cls.product_type,
            source_category=cls.product_class_name,
            source_url=_RESOURCE_CENTER_URL,
            hardware_revisions=tuple(hw_candidates),
        )


# ---------------------------------------------------------------------------
# 内部分组结构
# ---------------------------------------------------------------------------


class _ProductTree:
    """一棵产品的完整 Candidate 树（用于分页拉取时的中间状态）。"""

    def __init__(
        self,
        model_raw: str,
        model_normalized: str,
        classification: Classification,
    ) -> None:
        self.model_raw = model_raw
        self.model_normalized = model_normalized
        self.classification = classification
        # hw_normalized → _HwNode
        self.hardware_revisions: dict[str, _HwNode] = {}

    def add_artifact(
        self,
        hw_raw: str,
        hw_normalized: str,
        fw_version: str,
        release_date,
        title: str,
        api_id: str,
        download_url: str,
        doc_size_kb: int | None,
    ) -> None:
        hw_node = self.hardware_revisions.setdefault(
            hw_normalized, _HwNode(raw=hw_raw)
        )
        fw_node = hw_node.releases.setdefault(
            fw_version, _FwNode()
        )
        fw_node.artifacts.append(
            _ArtEntry(
                api_id=api_id,
                title=title,
                download_url=download_url,
                release_date=release_date,
                doc_size_kb=doc_size_kb,
            )
        )


class _HwNode:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        # fw_version → _FwNode
        self.releases: dict[str, _FwNode] = {}


class _FwNode:
    def __init__(self) -> None:
        self.artifacts: list[_ArtEntry] = []


class _ArtEntry:
    def __init__(
        self,
        api_id: str,
        title: str,
        download_url: str,
        release_date,
        doc_size_kb: int | None,
    ) -> None:
        self.api_id = api_id
        self.title = title
        self.download_url = download_url
        self.release_date = release_date
        self.doc_size_kb = doc_size_kb


# ---------------------------------------------------------------------------
# source_key 生成规则
# ---------------------------------------------------------------------------

def _make_hw_source_key(model: str, hw_normalized: str) -> str:
    """硬件版本 source_key：{model}/v{hw}。"""
    return f"{model}/v{hw_normalized}"


def _make_release_source_key(model: str, hw_normalized: str, fw_version: str) -> str:
    """固件发布 source_key：{model}/v{hw}/fw{fw_version}。"""
    return f"{model}/v{hw_normalized}/fw{fw_version}"


# ---------------------------------------------------------------------------
# 规范化辅助
# ---------------------------------------------------------------------------

def _normalize_model(model_raw: str) -> str:
    """型号规范化：去首尾空白，大写。保留中文变体。"""
    return model_raw.strip().upper()


def _normalize_hw_version(hw_raw: str) -> str:
    """硬件版本规范化：取第一个版本号作为规范化值。

    "1.0/V1.1" → "1.0"（固件兼容 V1.0 和 V1.1，以最早的为准）。
    """
    parts = hw_raw.strip().split("/")
    first = parts[0].strip()
    # 去掉可能残留的 V 前缀
    if first.upper().startswith("V"):
        first = first[1:]
    return first


def _extract_filename(url: str) -> str | None:
    """从 URL 提取文件名。"""
    try:
        return url.rsplit("/", 1)[-1] or None
    except (ValueError, IndexError):
        return None


def _kb_to_bytes(doc_size_kb: int | None) -> int | None:
    """docSize 单位为 KB，转换为字节。"""
    if doc_size_kb is None:
        return None
    return doc_size_kb * 1024
