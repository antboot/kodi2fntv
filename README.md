# kodi2fntv

Arctic Fuse 3 (纯皮肤版) + 飞牛影视(fnMedia) 插件 + 依赖，合成一个 Kodi 仓库。

## Kodi 安装

1. 设置 → 文件管理器 → 添加源 → 输入 `https://huahn3.github.io/kodi2fntv/`
2. 设置 → 插件 → 从 zip 文件安装 → 该源 → `repository.fuse.media/repository.fuse.media-1.0.0.zip`
3. 设置 → 插件 → 从仓库安装 → **Fuse Media Repository** → 安装皮肤/飞牛（依赖自动）

## 目录

- `skin.arctic.fuse.3/` 皮肤源码
- `plugin.video.fnmedia/` 飞牛插件源码
- `deps/` 依赖源码
- `tools/make_repo.py` 重新打包到 `docs/`
- `docs/` Kodi 库（GitHub Pages 从这里发布）

## 更新

```bash
python3 tools/make_repo.py            # 重新生成 docs/
git add -A && git commit -m "update repo" && git push
```
