# -*- coding: utf-8 -*-
"""飞牛影视与 Arctic Fuse 3 皮肤深度融合模块

功能：
1. 媒体库小组件同步：
   自动读取飞牛影视中的媒体库列表（mediadb/list），将主界面（homewidgets）
   小组件无缝替换为：
     - 第一项：继续观看（action=continue）
     - 其余项：用户在飞牛 NAS 中配置的媒体库（如电影、剧集、下载目录、自定义库等）
2. 动态检测变化：当媒体库新增、删除或重命名时，自动同步并触发皮肤小组件重新生成。
3. 异常与配对弹窗：提供类似 Emby for Kodi 的安装配置引导与断连/鉴权失败配对弹窗。
"""
import json
import os
import time
from urllib.parse import quote

import xbmc
import xbmcgui
import xbmcvfs

from resources.lib import util
from resources.lib.fnapi.client import ApiError

SKIN_ID = "skin.arctic.fuse.3"
_LAST_ERROR_DIALOG_TIME = 0
ERROR_DIALOG_COOLDOWN = 15  # 15秒内不重复弹窗提示配对，防止多小组件并行加载时弹窗刷屏


def is_fuse_skin_active():
    """判断当前激活的皮肤是否为 Arctic Fuse 3"""
    try:
        return xbmc.getSkinDir() == SKIN_ID
    except Exception:
        return False


def get_homewidgets_node_path():
    """获取 script.skinvariables 对应 Arctic Fuse 3 的用户数据节点路径"""
    return xbmcvfs.translatePath(
        "special://profile/addon_data/script.skinvariables/nodes/%s/skinvariables-shortcut-homewidgets.json" % SKIN_ID
    )


def get_homewidgets_skin_template_path():
    """获取 Arctic Fuse 3 皮肤自带的快捷方式模板路径"""
    return xbmcvfs.translatePath(
        "special://skin/shortcuts/skinvariables-shortcut-homewidgets.json"
    )


def build_fnmedia_homewidgets(client):
    """基于当前用户的飞牛媒体库生成主页小组件列表"""
    widgets = [
        {
            "label": "继续观看",
            "icon": "",
            "path": "plugin://%s/?action=continue" % util.ADDON_ID,
            "target": "videos",
            "guid": "guid-fnmedia-continue",
        }
    ]

    try:
        libraries = client.mediadb_list() or []
    except Exception as e:
        util.log("获取媒体库列表失败，跳过组件更新: %s" % e, xbmc.LOGWARNING)
        return None

    for mdb in libraries:
        # 跳过直播 IPTV 库
        if (mdb.get("category") or "").upper() == "IPTV":
            continue
        guid = mdb.get("guid")
        title = mdb.get("title") or "未命名库"
        if not guid:
            continue
        clean_guid = guid.replace("-", "")[:12]
        widgets.append({
            "label": title,
            "icon": "",
            "path": "plugin://%s/?action=filter&mdb=%s&title=%s" % (
                util.ADDON_ID, guid, quote(title)
            ),
            "target": "videos",
            "guid": "guid-fnmedia-%s" % clean_guid,
        })

    return widgets


_is_syncing = False
_last_sync_time = 0
SYNC_COOLDOWN = 15


def sync_home_widgets(force=False):
    """把飞牛媒体库与 Arctic Fuse 3 主页小组件同步。

    对比现有配置，若无变动且未强制刷新则不重复触发重载；
    若有变动则写入 userdata 节点文件并触发 script.skinvariables 编译。
    """
    global _is_syncing, _last_sync_time
    now = time.time()
    if _is_syncing:
        return False
    if not force and now - _last_sync_time < SYNC_COOLDOWN:
        return False

    _is_syncing = True
    try:
        server = util.get_setting("server")
        token = util.get_setting("token")
        if not server or not token:
            sync_skin_preferences()
            util.log("未配置飞牛服务器或未登录，跳过主页小组件同步", xbmc.LOGDEBUG)
            return False

        client = util.ensure_client()
        new_widgets = build_fnmedia_homewidgets(client)
        if not new_widgets:
            return False

        node_file = get_homewidgets_node_path()
        template_file = get_homewidgets_skin_template_path()

        # 检查当前配置是否一致
        current_widgets = []
        if os.path.exists(node_file):
            try:
                with open(node_file, "r", encoding="utf-8") as f:
                    current_widgets = json.load(f) or []
            except Exception:
                current_widgets = []

        def _sig(wlist):
            return [(w.get("label"), w.get("path")) for w in wlist if isinstance(w, dict)]

        if not force and _sig(current_widgets) == _sig(new_widgets):
            util.log("飞牛主页小组件与当前皮肤配置一致，无需重复更新", xbmc.LOGDEBUG)
            return False

        util.log("检测到飞牛媒体库变动或首次同步，正在更新 Arctic Fuse 3 主页小组件（共 %d 项）..." % len(new_widgets))

        try:
            os.makedirs(os.path.dirname(node_file), exist_ok=True)
            with open(node_file, "w", encoding="utf-8") as f:
                json.dump(new_widgets, f, ensure_ascii=False, indent=4)
        except Exception as e:
            util.log("写入小组件配置文件失败: %s" % e, xbmc.LOGERROR)
            return False

        # 若当前运行 Arctic Fuse 3，尝试同步更新皮肤默认模板，防止用户点击还原后变回默认
        if is_fuse_skin_active() and os.path.exists(os.path.dirname(template_file)):
            try:
                with open(template_file, "w", encoding="utf-8") as f:
                    json.dump(new_widgets, f, ensure_ascii=False, indent=4)
            except Exception:
                pass

        # 清除 Kodi Window 属性中的 shortcuts 内存缓存，通知 script.skinvariables 重新加载
        try:
            win = xbmcgui.Window(10000)
            win.clearProperty("SkinVariables.ShortcutsNode.%s-skinvariables-shortcut-homewidgets.json" % SKIN_ID)
            win.clearProperty("SkinVariables.ShortcutsNode.skinvariables-shortcut-homewidgets.json")
            xbmc.executebuiltin("SetProperty(SkinVariables.ShortcutsNode.Reload,%s,Home)" % time.time())
        except Exception:
            pass

        # 同步子菜单、默认关闭精选推荐及 Wall Flyer 视图类型
        sync_skin_preferences()

        _last_sync_time = time.time()

        if is_fuse_skin_active():
            # 异步触发 buildtemplate，避免阻塞 Kodi 调用方与双重 reload
            import threading
            def _trigger_rebuild():
                time.sleep(0.5)
                xbmc.executebuiltin("Skin.Reset(script-skinvariables-generator-hash)")
                xbmc.executebuiltin("RunScript(script.skinvariables,action=buildtemplate,force=True)")
            threading.Thread(target=_trigger_rebuild, daemon=True).start()
            util.notify("已深度融合飞牛媒体库至主页小组件")

        return True
    finally:
        _is_syncing = False


def sync_skin_preferences():
    """同步皮肤配置：
    1. 子菜单去掉音乐与图片，保留视频和飞牛影视
    2. 精选推荐（Spotlight）默认关闭
    3. 飞牛影视及媒体库相关视图统一配置为 Wall Flyer (516)
    """
    if not is_fuse_skin_active():
        return

    # 1. 调整 homesubmenu
    submenu_node = xbmcvfs.translatePath(
        "special://profile/addon_data/script.skinvariables/nodes/%s/skinvariables-shortcut-homesubmenu.json" % SKIN_ID
    )
    desired_submenu = [
        {
            "label": "$LOCALIZE[3]",
            "icon": "special://skin/extras/icons/video.png",
            "path": "ActivateWindow(videos)",
            "target": "",
            "guid": "guid-0edddec3",
        },
        {
            "label": "fntv",
            "path": "plugin://%s" % util.ADDON_ID,
            "icon": "special://home/addons/%s/resources/icon.png" % util.ADDON_ID,
            "target": "videos",
            "guid": "guid-f32d3228",
        },
    ]
    try:
        current_sub = []
        if os.path.exists(submenu_node):
            with open(submenu_node, "r", encoding="utf-8") as f:
                current_sub = json.load(f) or []
        sig_current = [(item.get("label"), item.get("path")) for item in current_sub if isinstance(item, dict)]
        sig_desired = [(item.get("label"), item.get("path")) for item in desired_submenu]
        if sig_current != sig_desired:
            os.makedirs(os.path.dirname(submenu_node), exist_ok=True)
            with open(submenu_node, "w", encoding="utf-8") as f:
                json.dump(desired_submenu, f, ensure_ascii=False, indent=4)
            win = xbmcgui.Window(10000)
            win.clearProperty("SkinVariables.ShortcutsNode.%s-skinvariables-shortcut-homesubmenu.json" % SKIN_ID)
            win.clearProperty("SkinVariables.ShortcutsNode.skinvariables-shortcut-homesubmenu.json")
    except Exception as e:
        util.log("sync_homesubmenu error: %s" % e, xbmc.LOGDEBUG)

    # 2. 精选推荐默认关闭
    try:
        xbmc.executebuiltin("Skin.SetString(HomeSwitcher.Home.Spotlight.Path,noop)")
        xbmc.executebuiltin("Skin.Reset(HomeSwitcher.Home.Spotlight.Target)")
        xbmc.executebuiltin("Skin.SetString(HomeSwitcher.Home.Spotlight.Label,None)")
    except Exception:
        pass

    # 3. 视图类型配置 (Wall Flyer 516)
    viewtypes_file = xbmcvfs.translatePath(
        "special://profile/addon_data/script.skinvariables/%s-viewtypes.json" % SKIN_ID
    )
    try:
        vt_data = {}
        if os.path.exists(viewtypes_file):
            with open(viewtypes_file, "r", encoding="utf-8") as f:
                vt_data = json.load(f) or {}

        changed = False
        target_keys = ["movies", "sets", "tvshows", "episode-groups-tvshows", "episode-groups-seasons", "seasons", "videoversions", "actors"]

        for section in ["library", "plugins"]:
            if section in vt_data:
                for k in target_keys:
                    if vt_data[section].get(k) != "516":
                        vt_data[section][k] = "516"
                        changed = True

        fn_rules = vt_data.setdefault(util.ADDON_ID, {})
        for k in target_keys:
            if fn_rules.get(k) != "516":
                fn_rules[k] = "516"
                changed = True
        fn_rules.setdefault("files", "510")
        fn_rules.setdefault("addons", "513")

        if changed:
            with open(viewtypes_file, "w", encoding="utf-8") as f:
                json.dump(vt_data, f, ensure_ascii=False, indent=4)
            xbmc.executebuiltin("RunScript(script.skinvariables,action=buildviews)")
    except Exception as e:
        util.log("sync_viewtypes error: %s" % e, xbmc.LOGDEBUG)



def prompt_first_run_setup():
    """安装后或首次运行：检测到未配置服务器时主动弹窗引导配置"""
    if util.get_setting("server"):
        return False

    dialog = xbmcgui.Dialog()
    ok = dialog.yesno(
        "飞牛影视 - 欢迎使用",
        "检测到尚未配置飞牛 NAS 服务器。\n是否立即打开设置窗口进行配对与配置？",
        yeslabel="立即配置",
        nolabel="稍后再说",
    )
    if ok:
        xbmc.executebuiltin("Addon.OpenSettings(%s)" % util.ADDON_ID)
        return True
    return False


def prompt_connection_failure(error_msg="无法连接到服务器"):
    """类似 Emby for Kodi：连不上服务器或授权失效时弹窗提示并引导重新配对"""
    global _LAST_ERROR_DIALOG_TIME
    now = time.time()
    if now - _LAST_ERROR_DIALOG_TIME < ERROR_DIALOG_COOLDOWN:
        util.notify(str(error_msg), error=True)
        return False

    _LAST_ERROR_DIALOG_TIME = now
    server = util.get_setting("server") or "未配置"
    msg = (
        "无法连接到飞牛影视服务器 (%s)：\n\n"
        "%s\n\n"
        "是否重新检查服务器地址、密码或重新配对？"
        % (server, str(error_msg))
    )
    dialog = xbmcgui.Dialog()
    if dialog.yesno("飞牛影视 - 连接异常", msg, yeslabel="重新设置", nolabel="取消"):
        xbmc.executebuiltin("Addon.OpenSettings(%s)" % util.ADDON_ID)
        return True
    return False
