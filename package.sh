#!/usr/bin/env bash
# 飞牛影视 Kodi 插件打包脚本
#
#   ./package.sh              打包到仓库同级目录（默认 ~/Desktop）
#   ./package.sh ~/Downloads  打包到指定目录
#
# 产物：<输出目录>/plugin.video.fnmedia-<addon.xml 里的版本>.zip
# Kodi「从 zip 文件安装加载项」要求归档内只有一个顶层目录 plugin.video.fnmedia/，
# 平铺打包会被 Kodi 直接拒绝（真机 kodi.log 里报 "first item is folder: false"），
# 所以这里用暂存目录重排，并在打包后强校验结构。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADDON_ID="plugin.video.fnmedia"
OUT_DIR="${1:-$(dirname "$REPO")}"
XML="$REPO/addon.xml"

die() { echo "✗ 打包失败：$*" >&2; exit 1; }

[ -f "$XML" ] || die "找不到 $XML（请在插件仓库根目录运行）"
VERSION="$(python3 -c "import xml.etree.ElementTree as E;print(E.parse('$XML').getroot().get('version'))")" \
    || die "addon.xml 解析失败"
[ -n "$VERSION" ] || die "addon.xml 没有 version 属性"
OUT="$OUT_DIR/$ADDON_ID-$VERSION.zip"
mkdir -p "$OUT_DIR"

# ---------- 1. 三道闸：语法 / 导入 / 必备文件 ----------
echo "① 语法检查"
FIND_ARGS=(-name '*.py' -not -path '*/.git/*' -not -path '*/__pycache__/*')
find "$REPO" "${FIND_ARGS[@]}" -print0 | python3 -c '
import sys
bad = []
for p in sys.stdin.read().split("\0"):
    if not p:
        continue
    try:
        compile(open(p, encoding="utf-8").read(), p, "exec")
    except SyntaxError as e:
        bad.append("%s:%s  %s" % (e.filename, e.lineno, e.msg))
if bad:
    print("\n".join(bad), file=sys.stderr)
    sys.exit(1)
' || die "有 .py 语法错误，先修掉"
echo "   ✓ $(find "$REPO" "${FIND_ARGS[@]}" | wc -l | tr -d ' ') 个 .py 语法正确"

echo "② 依赖自检（import 到的模块文件都还在）"
python3 - "$REPO" <<'PY' || die "有模块被删了但还在被 import，先改干净"
import os, re, sys
root = sys.argv[1]
bad = []
for dp, dn, fn in os.walk(root):
    dn[:] = [d for d in dn if d not in ('.git', '.qoder', '.idea', '__pycache__')]
    for name in fn:
        if not name.endswith('.py'):
            continue
        path = os.path.join(dp, name)
        text = open(path, encoding='utf-8').read()
        for m in re.finditer(r'(?:from|import)\s+(resources\.lib(?:\.[a-z_]+)*)'
                             r'(?:\s+import\s+([^#\n]*))?', text):
            pkg, imported = m.group(1), m.group(2) or ''
            if pkg.count('.') == 1 and imported:
                # from resources.lib import browser, util as u  → 每个都得是模块文件
                mods = [pkg + '.' + it.split()[0]
                        for it in re.split(r'[,()]', imported)
                        if it.split() and it.split()[0].isidentifier()]
            else:
                mods = [pkg]          # from resources.lib.x import y → 只查 x.py 在不在
            for mod in mods:
                f = os.path.join(root, *mod.split('.')) + '.py'
                if not (os.path.isfile(f) or os.path.isfile(os.path.join(root, *mod.split('.'), '__init__.py'))):
                    bad.append('%s → %s' % (os.path.relpath(path, root), mod))
if bad:
    print('\n'.join('   ✗ ' + b for b in sorted(set(bad))), file=sys.stderr)
    sys.exit(1)
PY
echo "   ✓ 模块引用完整"

for f in addon.py addon.xml resources/settings.xml resources/icon.png \
         resources/lib/browser.py resources/lib/desc.py resources/lib/meta.py \
         resources/lib/player.py resources/lib/proxy.py resources/lib/service.py \
         resources/lib/sniff.py resources/lib/util.py resources/lib/store.py \
         resources/lib/fnapi/auth.py resources/lib/fnapi/client.py; do
    [ -f "$REPO/$f" ] || die "缺文件 $f"
done
echo "③ 必备文件齐全 ✓"

# ---------- 2. 暂存成 Kodi 认的单顶层结构 ----------
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$ADDON_ID"
rsync -a \
    --exclude '.git' --exclude '.qoder' --exclude '.idea' --exclude '.DS_Store' \
    --exclude '__pycache__' --exclude '*.pyc' --exclude '*.pyo' --exclude '*.zip' \
    --exclude '.agents' --exclude 'package.sh' \
    "$REPO/" "$STAGE/$ADDON_ID/"

# ---------- 3. 打包 ----------
rm -f "$OUT"
(cd "$STAGE" && zip -r -X -q "$OUT" "$ADDON_ID")

# ---------- 4. 校验产物 ----------
TOPS="$(unzip -Z1 "$OUT" | awk -F/ '{print $1}' | sort -u)"
[ "$TOPS" = "$ADDON_ID" ] || die "顶层必须且只能有一个 $ADDON_ID/ 目录，实际：$(echo "$TOPS" | tr '\n' ' ')"
LIST="$(unzip -Z1 "$OUT")"
for junk in '.git/' '__pycache__' '.qoder' '.pyc'; do
    if printf '%s\n' "$LIST" | grep -q -- "$junk"; then
        die "包里混进了 $junk"
    fi
done
ZIP_VER="$(unzip -p "$OUT" "$ADDON_ID/addon.xml" |
    python3 -c "import sys,xml.etree.ElementTree as E;print(E.parse(sys.stdin).getroot().get('version'))")"
[ "$ZIP_VER" = "$VERSION" ] || die "包内版本 $ZIP_VER ≠ 期望 $VERSION"
FILES="$(printf '%s\n' "$LIST" | grep -vc '/$')"
SIZE="$(du -h "$OUT" | cut -f1 | tr -d ' ')"

echo
echo "✓ 打包完成"
echo "  文件    $OUT"
echo "  版本    $ZIP_VER    $FILES 个文件    $SIZE"
echo
echo "安装：Kodi → 文件管理器 → 加载项 → 从 zip 文件安装 → 选上面这个 zip"
echo "提示：改完代码记得抬 addon.xml 里的 version，否则设备上分不清新旧"
