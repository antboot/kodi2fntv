#!/usr/bin/env python3
"""Build the Kodi repository into ./docs (served by GitHub Pages).

Usage:
    python3 tools/make_repo.py [BASE_URL]

BASE_URL defaults to https://huahn3.github.io/kodi2fntv
Run from anywhere; paths are resolved relative to this file.
"""
import os, re, sys, shutil, subprocess, hashlib, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs")
STAGE = os.path.join(ROOT, ".repo_stage")
BASEURL = (sys.argv[1] if len(sys.argv) > 1 else "https://fntv.huhan1.de5.net").rstrip("/")
# Where Kodi fetches addons.xml / addons.xml.md5 from.
DATAURL = (sys.argv[2] if len(sys.argv) > 2 else BASEURL).rstrip("/")

# addon id -> source directory
SOURCES = {
    "skin.arctic.fuse.3": os.path.join(ROOT, "skin.arctic.fuse.3"),
    "plugin.video.fnmedia": os.path.join(ROOT, "plugin.video.fnmedia"),
    "script.skinvariables": os.path.join(ROOT, "deps", "script.skinvariables"),
    "script.module.jurialmunkey": os.path.join(ROOT, "deps", "script.module.jurialmunkey"),
    "script.module.infotagger": os.path.join(ROOT, "deps", "script.module.infotagger"),
    "script.module.requests": os.path.join(ROOT, "deps", "script.module.requests"),
    "script.module.certifi": os.path.join(ROOT, "deps", "script.module.certifi"),
    "script.module.chardet": os.path.join(ROOT, "deps", "script.module.chardet"),
    "script.module.idna": os.path.join(ROOT, "deps", "script.module.idna"),
    "script.module.urllib3": os.path.join(ROOT, "deps", "script.module.urllib3"),
    "resource.images.weathericons.white": os.path.join(ROOT, "deps", "resource.images.weathericons.white"),
    "resource.images.studios.coloured": os.path.join(ROOT, "deps", "resource.images.studios.coloured"),
    "resource.font.robotocjksc": os.path.join(ROOT, "deps", "resource.font.robotocjksc"),
}

REPO_ID = "repository.kodi2fntv.media"
REPO_VER = "1.0.4"

EXCLUDE = ["--exclude", ".git", "--exclude", ".github", "--exclude", ".gitignore",
           "--exclude", ".gitattributes", "--exclude", ".agents", "--exclude", ".DS_Store",
           "--exclude", "__pycache__", "--exclude", "*.pyc", "--exclude", "*.zip"]


def read_version(ax):
    s = open(ax, encoding="utf-8", errors="ignore").read()
    return re.search(r'<addon[^>]*\bversion="([^"]+)"', s).group(1)


def strip_decl(s):
    return re.sub(r'^\s*<\?xml[^>]*\?>\s*', '', s, count=1)


def make_zip(basedir, outfile):
    with zipfile.ZipFile(outfile, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(basedir):
            dirs.sort()
            for fn in sorted(files):
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, os.path.dirname(basedir)).replace(os.sep, "/")
                zi = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = 0o100644 << 16
                with open(full, "rb") as fh:
                    z.writestr(zi, fh.read())


if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)
open(os.path.join(OUT, ".nojekyll"), "w").close()

blocks = []
for aid, src in SOURCES.items():
    ax = os.path.join(src, "addon.xml")
    if not os.path.isfile(ax):
        print("SKIP (no addon.xml):", aid); continue
    ver = read_version(ax)
    if os.path.isdir(STAGE):
        shutil.rmtree(STAGE, ignore_errors=True)
    dst = os.path.join(STAGE, aid)
    os.makedirs(dst)
    subprocess.run(["rsync", "-a"] + EXCLUDE + [src + "/", dst + "/"], check=True)
    zpath = os.path.join(OUT, aid, aid + "-" + ver + ".zip")
    os.makedirs(os.path.dirname(zpath))
    make_zip(dst, zpath)
    for asset in ["icon.png", "fanart.jpg", "fanart.png"]:
        src_asset = os.path.join(src, asset)
        if not os.path.isfile(src_asset):
            src_asset = os.path.join(src, "resources", asset)
        if os.path.isfile(src_asset):
            shutil.copy2(src_asset, os.path.join(OUT, aid, asset))
    blocks.append(strip_decl(open(ax, encoding="utf-8", errors="ignore").read()).strip())
    print("packed %s-%s.zip" % (aid, ver))

# repository addon
repo_dir = os.path.join(STAGE, REPO_ID)
os.makedirs(repo_dir)
repo_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="%s" name="Fuse Media Repository" version="%s" provider-name="huahn3">
    <extension point="xbmc.addon.repository" name="Fuse Media Repository">
        <dir>
            <info compressed="false">%s/addons.xml</info>
            <checksum>%s/addons.xml.md5</checksum>
            <datadir zip="true">%s/</datadir>
        </dir>
    </extension>
    <extension point="xbmc.addon.metadata">
        <summary lang="en_GB">Fuse Media Repository</summary>
        <summary lang="zh_CN">Fuse 媒体仓库</summary>
        <description lang="en_GB">Arctic Fuse 3 (pure) skin, fnOS media plugin and their dependencies.</description>
        <description lang="zh_CN">Arctic Fuse 3（纯皮肤）、飞牛影视插件及其全部依赖。</description>
        <platform>all</platform>
    </extension>
</addon>
''' % (REPO_ID, REPO_VER, DATAURL, DATAURL, DATAURL)
open(os.path.join(repo_dir, "addon.xml"), "w", encoding="utf-8").write(repo_xml)
os.makedirs(os.path.join(OUT, REPO_ID))
make_zip(repo_dir, os.path.join(OUT, REPO_ID, REPO_ID + "-" + REPO_VER + ".zip"))
shutil.copy2(os.path.join(OUT, REPO_ID, REPO_ID + "-" + REPO_VER + ".zip"),
             os.path.join(OUT, REPO_ID + "-" + REPO_VER + ".zip"))
blocks.append(strip_decl(repo_xml).strip())
print("packed", REPO_ID + "-" + REPO_VER + ".zip")

axml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n' + "\n".join(blocks) + '\n</addons>\n'
open(os.path.join(OUT, "addons.xml"), "w", encoding="utf-8").write(axml)
open(os.path.join(OUT, "addons.xml.md5"), "w").write(hashlib.md5(axml.encode()).hexdigest())


def write_index(dirpath, links):
    """Write an Apache-autoindex-like listing so Kodi's HTTP browser can parse it."""
    rows = "\n".join('<a href="%s">%s</a>' % (u, n) for n, u in links)
    html = ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>%s</title></head>'
            '<body><h1>Index of /%s</h1><hr><pre>%s</pre><hr></body></html>'
            % (os.path.basename(dirpath) or "", os.path.basename(dirpath) or "", rows))
    open(os.path.join(dirpath, "index.html"), "w", encoding="utf-8").write(html)


# index.html per addon folder -> link to its zip(s)
for aid in list(SOURCES.keys()) + [REPO_ID]:
    d = os.path.join(OUT, aid)
    if not os.path.isdir(d):
        continue
    zips = sorted(f for f in os.listdir(d) if f.endswith(".zip"))
    write_index(d, [(z, z) for z in zips] + [("..", "../")])

# root index.html -> the repository zip directly (Kodi installs it without entering a folder);
# addon folders still exist and are fetched by Kodi via direct URLs, not by browsing.
write_index(OUT, [(REPO_ID + "-" + REPO_VER + ".zip", REPO_ID + "-" + REPO_VER + ".zip")])
if os.path.isdir(STAGE):
    shutil.rmtree(STAGE, ignore_errors=True)
print("BASEURL =", BASEURL)
print("docs/ built, addons:", len(blocks))
