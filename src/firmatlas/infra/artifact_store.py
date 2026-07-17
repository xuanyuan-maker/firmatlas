"""固件本地归档（接口设计 §8）。

- 构造归档路径：厂商/地区/型号/硬件版本/固件版本/文件
- 路径片段安全规范化（禁止 ..、路径分隔符注入、不可打印字符）
- 文件名添加短 Artifact ID 前缀避免同名冲突
- 校验通过后原子移动（os.rename 同文件系统内保证原子性）
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PurePosixPath

from firmatlas.domain.model import ArtifactContext

# 允许的字符集：字母、数字、连字符、下划线、点、空格
_SAFE_CHAR = re.compile(r"[^a-zA-Z0-9\-_. ]")

# 多连字符压缩
_MULTI_DASH = re.compile(r"-{2,}")

# Artifact ID 短前缀长度
_ID_PREFIX_LEN = 8

# 厂商/地区/型号/硬件版本/固件版本/文件 各段最大长度（字节），防止过长路径
# POSIX 路径最大 255 字节/段；我们取保守值 128
_MAX_SEGMENT_LEN = 128


class ArtifactStore:
    """同步的本地归档管理器。

    用法：
        store = ArtifactStore(data_dir=Path("data"))
        relative = store.build_final_relative_path(ctx, original_filename)
        store.promote(tmp_path=tmp, final_relative_path=relative)
    """

    def __init__(self, data_dir: Path) -> None:
        self._firmware_dir = data_dir / "firmware"

    def build_final_relative_path(
        self, ctx: ArtifactContext, original_filename: str | None
    ) -> PurePosixPath:
        """构造归档相对路径，不做校验（调用方在 promote 时创建目录）。

        格式：厂商/地区/型号/硬件版本/固件版本/{短ID}__{文件名}
        """
        vendor = _sanitize(ctx.source.vendor_key)
        region = _sanitize(ctx.source.region_code)
        model = _sanitize(ctx.product.model_normalized)
        hw = _sanitize(ctx.hardware_revision.normalized_revision)
        fw = (
            _sanitize(ctx.release.version_normalized)
            if ctx.release.version_normalized
            else _sanitize(ctx.release.version_raw)
        )

        filename = _build_filename(ctx.artifact.id, original_filename)
        return PurePosixPath(vendor) / region / model / hw / fw / filename

    def promote(self, *, tmp_path: Path, final_relative_path: PurePosixPath) -> Path:
        """将临时文件原子移动到最终归档路径。

        同一文件系统内 os.rename 保证原子性；跨文件系统时回退到 shutil.move。
        目标目录自动创建。
        """
        dest = self._firmware_dir / final_relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(tmp_path, dest)
        except OSError:
            # 跨文件系统时 os.rename 会报 EXDEV；回退到 shutil.move
            shutil.move(str(tmp_path), str(dest))
        return dest


def _sanitize(segment: str) -> str:
    """安全规范化路径片段：替换非法字符、消除路径遍历、压缩连字符、截断长度。

    空串/全被替换的串回退为 '_'，避免出现空路径段。
    """
    # 1. 替换 .. 和 .（独立的点段）——消除路径遍历
    cleaned = re.sub(r"\.\.+", "-", segment)
    # 2. 非允许字符 → 连字符
    cleaned = _SAFE_CHAR.sub("-", cleaned)
    # 3. 压缩并去除首尾的非字母数字
    cleaned = _MULTI_DASH.sub("-", cleaned).strip(" -.")
    if not cleaned:
        cleaned = "_"
    # 4. 按 UTF-8 字节截断（避免截在 Unicode 中间）
    encoded = cleaned.encode("utf-8")
    if len(encoded) > _MAX_SEGMENT_LEN:
        truncated = encoded[:_MAX_SEGMENT_LEN]
        cleaned = truncated.decode("utf-8", errors="ignore")
    return cleaned


def _build_filename(artifact_id: str, original_filename: str | None) -> str:
    """构造归档文件名：{短ID}__{安全文件名}。

    不信任服务器文件名——任何不符合安全规范的字符被替换为 '-'。
    没有原始文件名时只用短 ID。总长度超限时优先保留前缀。
    """
    prefix = artifact_id[:_ID_PREFIX_LEN]
    if original_filename:
        # 只保留最后一个路径段的文件名（防止 URL 路径注入）
        base = Path(original_filename).name
        safe = _sanitize(base)
        full = f"{prefix}__{safe}"
        # 确保总长度不超标（优先保留前缀 + __）
        if len(full.encode("utf-8")) > _MAX_SEGMENT_LEN:
            max_safe = _MAX_SEGMENT_LEN - len(f"{prefix}__".encode())
            if max_safe <= 0:
                return prefix
            safe_encoded = safe.encode("utf-8")[:max_safe]
            safe = safe_encoded.decode("utf-8", errors="ignore").rstrip(" -.")
            full = f"{prefix}__{safe}"
        return full
    return prefix
