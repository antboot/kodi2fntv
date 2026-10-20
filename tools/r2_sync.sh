#!/bin/zsh
# Incremental sync: docs/ -> Cloudflare R2 bucket (served at https://fntv.qq47.de5.net/)
# Requires: npx wrangler logged in (npx wrangler login), macOS (md5/stat).
set -euo pipefail
export PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
export WRANGLER_SEND_METRICS=false

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCS="$ROOT/docs"
BUCKET="kodi2fntv-apac"
MANIFEST="$ROOT/tools/.r2-manifest.tsv"

[ -d "$DOCS" ] || { echo "docs/ not found; run: python3 tools/make_repo.py" >&2; exit 1; }

upload_file() {
  local file="$1" key="$2" ct cc
  case "$file" in
    *.zip) ct="application/zip"; cc="public, max-age=31536000, immutable";;
    *.html) ct="text/html; charset=utf-8"; cc="public, max-age=60";;
    *.xml) ct="application/xml; charset=utf-8"; cc="public, max-age=60";;
    *.md5) ct="text/plain; charset=utf-8"; cc="public, max-age=60";;
    *.png) ct="image/png"; cc="public, max-age=31536000, immutable";;
    *.jpg|*.jpeg) ct="image/jpeg"; cc="public, max-age=31536000, immutable";;
    *) ct="application/octet-stream"; cc="public, max-age=60";;
  esac
  npx --yes wrangler r2 object put "$BUCKET/$key" --file="$file" \
    --content-type="$ct" --cache-control="$cc" --remote >/dev/null
}

typeset -A OLD_MD5
if [ -f "$MANIFEST" ]; then
  while IFS=$'\t' read -r k _s m; do
    [ -n "${k:-}" ] && OLD_MD5[$k]="$m"
  done < "$MANIFEST"
fi

typeset -A NEW_MD5
NEW_MANIFEST="$(mktemp)"
uploaded=0 skipped=0

cd "$DOCS"
while IFS= read -r f; do
  key="${f#./}"
  md5="$(md5 -q "$f")"
  NEW_MD5[$key]="$md5"
  if [ "${OLD_MD5[$key]:-}" = "$md5" ]; then
    skipped=$((skipped+1))
  else
    printf 'upload  %s\n' "$key"
    upload_file "$f" "$key"
    uploaded=$((uploaded+1))
  fi
  printf '%s\t%s\t%s\n' "$key" "$(stat -f%z "$f")" "$md5" >> "$NEW_MANIFEST"
done < <(find . -type f ! -path './.wrangler/*' ! -name '.nojekyll' ! -name '.DS_Store' | sort)

deleted=0
for k in ${(k)OLD_MD5}; do
  if [ -z "${NEW_MD5[$k]:-}" ]; then
    printf 'delete  %s\n' "$k"
    npx --yes wrangler r2 object delete "$BUCKET/$k" --remote >/dev/null
    deleted=$((deleted+1))
  fi
done

mv "$NEW_MANIFEST" "$MANIFEST"
echo "sync done: uploaded=$uploaded skipped=$skipped deleted=$deleted"
