#!/usr/bin/env bash
set -Eeuo pipefail

output_dir="${1:?用法：publish-catalog-release.sh OUTPUT_DIR REPOSITORY}"
repository="${2:?用法：publish-catalog-release.sh OUTPUT_DIR REPOSITORY}"
manifest="$output_dir/manifest.json"

command -v gh >/dev/null || { echo "需要 gh CLI 发布 GitHub Release。" >&2; exit 1; }
command -v jq >/dev/null || { echo "需要 jq 读取 catalog_version。" >&2; exit 1; }

catalog_version="$(jq -r '.catalog_version' "$manifest")"
tag="catalog-v$catalog_version"
gh release create "$tag" \
  --repo "$repository" \
  --title "$tag" \
  --notes "FirmAtlas Catalog $catalog_version" \
  "$output_dir/manifest.json" \
  "$output_dir/firmatlas.db.gz" \
  "$output_dir/firmatlas.db.gz.sha256"

gh release view "$tag" --repo "$repository" --json assets \
  --jq '.assets | map(.name) | sort | join("\\n")' | \
  grep -Fx 'manifest.json' >/dev/null
gh release view "$tag" --repo "$repository" --json assets \
  --jq '.assets | map(.name) | sort | join("\\n")' | \
  grep -Fx 'firmatlas.db.gz' >/dev/null
gh release view "$tag" --repo "$repository" --json assets \
  --jq '.assets | map(.name) | sort | join("\\n")' | \
  grep -Fx 'firmatlas.db.gz.sha256' >/dev/null

echo "Catalog Release 发布并验证：$repository $tag"
