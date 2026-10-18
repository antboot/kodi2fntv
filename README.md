# kodi2fntv

Arctic Fuse 3 (纯皮肤版) + 飞牛影视(fnMedia) 插件 + 依赖，合成一个 Kodi 仓库。

## Kodi 安装

1. 设置 → 文件管理器 → 添加源 → 输入 `https://fntv.huhan1.de5.net/`（备用源：`https://huahn3.github.io/kodi2fntv/`）
2. 设置 → 插件 → 从 zip 文件安装 → 该源 → `repository.kodi2fntv.media-1.0.4.zip`（根目录直接显示）
3. 设置 → 插件 → 从仓库安装 → **Fuse Media Repository** → 安装皮肤/飞牛（依赖自动）

## 目录

- `skin.arctic.fuse.3/` 皮肤源码
- `plugin.video.fnmedia/` 飞牛插件源码
- `deps/` 依赖源码
- `tools/make_repo.py` 重新打包到 `docs/`
- `tools/r2_sync.sh` 增量同步 `docs/` 到 Cloudflare R2（主源）
- `docs/` Kodi 库（R2 主源，GitHub Pages 备用）

## 更新

```bash
python3 tools/make_repo.py            # 重新生成 docs/
git add -A && git commit -m "update repo" && git push
zsh tools/r2_sync.sh                  # 增量同步到 R2（只上传变化的文件）
```
