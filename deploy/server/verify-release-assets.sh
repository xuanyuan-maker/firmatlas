#!/usr/bin/env bash
set -Eeuo pipefail

output_dir="${1:?用法：verify-release-assets.sh OUTPUT_DIR}"
manifest="$output_dir/manifest.json"
database="$output_dir/firmatlas.db.gz"
checksum="$output_dir/firmatlas.db.gz.sha256"

for path in "$manifest" "$database" "$checksum"; do
  [[ -f "$path" ]] || { echo "缺少发布资产：$path" >&2; exit 1; }
done

command -v jq >/dev/null || { echo "需要 jq 校验 manifest。" >&2; exit 1; }
command -v gzip >/dev/null || { echo "需要 gzip 校验数据库。" >&2; exit 1; }

jq -e '.format_version == 1 and .database.compression == "gzip" and .counts.downloads == 0' \
  "$manifest" >/dev/null
sha256sum --check "$checksum" >/dev/null
gzip -t "$database"

echo "Catalog 发布资产校验通过：$output_dir"
