#!/usr/bin/env bash
set -Eeuo pipefail

# 服务器单次采集周期：同一数据目录只允许一个周期运行。
# 凭据只从 systemd EnvironmentFile/受保护环境注入，绝不写入脚本或快照。

FIRMATLAS_BIN="${FIRMATLAS_BIN:-firmatlas}"
FIRMATLAS_DATA_DIR="${FIRMATLAS_DATA_DIR:-/var/lib/firmatlas}"
FIRMATLAS_EXPORT_ROOT="${FIRMATLAS_EXPORT_ROOT:-/var/lib/firmatlas/exports}"
FIRMATLAS_CATALOG_REPO="${FIRMATLAS_CATALOG_REPO:-xuanyuan-maker/firmatlas-catalog}"
FIRMATLAS_ALLOW_PARTIAL="${FIRMATLAS_ALLOW_PARTIAL:-false}"
FIRMATLAS_EXPORT_RETENTION="${FIRMATLAS_EXPORT_RETENTION:-5}"
FIRMATLAS_SOURCES="${FIRMATLAS_SOURCES:-tp-link-cn tp-link-us hikvision-global dlink-us omada-global zyxel-global dahua-global draytek-global miwifi-cn tenda-global uniview-global ruijie-cn}"

mkdir -p "$FIRMATLAS_DATA_DIR" "$FIRMATLAS_EXPORT_ROOT"
exec 9>"$FIRMATLAS_DATA_DIR/.catalog-cycle.lock"
flock -n 9 || {
  echo "已有采集/导出周期运行，退出。" >&2
  exit 75
}

"$FIRMATLAS_BIN" --data-dir "$FIRMATLAS_DATA_DIR" init

failed_sources=()
for source_key in $FIRMATLAS_SOURCES; do
  if ! "$FIRMATLAS_BIN" --data-dir "$FIRMATLAS_DATA_DIR" crawl "$source_key"; then
    failed_sources+=("$source_key")
    echo "来源失败：$source_key" >&2
  fi
done

if ((${#failed_sources[@]} > 0)) && [[ "$FIRMATLAS_ALLOW_PARTIAL" != "true" ]]; then
  echo "有来源失败，当前策略不允许发布：${failed_sources[*]}" >&2
  exit 1
fi

catalog_version="$(date -u +%Y.%m.%d.%H%M%S)"
output_dir="$FIRMATLAS_EXPORT_ROOT/catalog-$catalog_version"
report_file="$FIRMATLAS_EXPORT_ROOT/catalog-$catalog_version.json"
"$FIRMATLAS_BIN" --data-dir "$FIRMATLAS_DATA_DIR" catalog export \
  --output "$output_dir" --format json >"$report_file"

"$(dirname "$0")/verify-release-assets.sh" "$output_dir"
"$(dirname "$0")/publish-catalog-release.sh" "$output_dir" "$FIRMATLAS_CATALOG_REPO"

# 导出目录是可重建的发布缓存，只保留最近 N 个；规范数据库备份不在此处清理。
if [[ "$FIRMATLAS_EXPORT_RETENTION" =~ ^[1-9][0-9]*$ ]]; then
  mapfile -t old_exports < <(
    find "$FIRMATLAS_EXPORT_ROOT" -mindepth 1 -maxdepth 1 -type d -name 'catalog-*' \
      -printf '%T@ %p\n' | sort -rn | tail -n +$((FIRMATLAS_EXPORT_RETENTION + 1)) | cut -d' ' -f2-
  )
  for old_export in "${old_exports[@]}"; do
    [[ -n "$old_export" ]] && rm -rf -- "$old_export"
  done
else
  echo "忽略无效的 FIRMATLAS_EXPORT_RETENTION：$FIRMATLAS_EXPORT_RETENTION" >&2
fi
