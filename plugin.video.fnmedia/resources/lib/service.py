# -*- coding: utf-8 -*-
"""后台服务：把 Kodi 侧观看记录回传给 NAS（进度 + 已看），并常驻预热海报墙缓存

插件在开始播放时写入 pending.json（含各 guid 与流地址），本服务常驻运行，
确认播放的是本插件的代理流后：播放中每 60 秒 POST /v/api/v1/play/record
回传进度（崩溃/强退最多丢一个间隔），停止或播完时回传最终位置，接近播完
另 POST /v/api/v1/item/watched 标记已看——飞牛 App/网页据此续播，跨端一致。
回传失败保留 pending 重试，Kodi 退出前先落一次位置。
"""
import os
import sys
import time

# Kodi 以本文件为入口启动时，sys.path 只有 resources/lib，
# 需要把插件根目录加进来才能 import resources.*
_ADDON_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ADDON_ROOT not in sys.path:
    sys.path.insert(0, _ADDON_ROOT)

import xbmc

from resources.lib import util
from resources.lib.player import PENDING_FILE

MIN_PROGRESS_SECONDS = 5  # 播放不足 5 秒不上报
RESUME_SEEK_THRESHOLD = 10  # 续播点不足 10s 不自动 seek（接近开头，直接从 0 看）
PROGRESS_REPORT_INTERVAL = 60   # 播放中周期回传间隔：崩溃/断电最多丢一个间隔
REPORT_RETRY_S = 30             # 最终回传失败后的重试间隔
REPORT_RETRY_MAX = 3            # 重试次数上限，超过则保留 pending 等下次启动补报


def _to_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _norm_stream_url(url):
    """播放 URL 匹配用：去掉 Kodi 管道参数（| 及其后）。

    网盘直链播放地址可能带 |User-Agent=..&Cookie=.. 注入段，不同 Kodi 版本
    的 getPlayingFile() 对管道段的返回不一致（含/不含），统一按去管道后的
    纯 URL 比较，避免直链播放时进度回传/自动续播静默失效。"""
    return (url or '').split('|', 1)[0]


class FnPlayer(xbmc.Player):
    def __init__(self):
        super(FnPlayer, self).__init__()
        self.pending = None        # 当前待上报的播放信息
        self.ours_playing = False  # 是否确认是本插件的流在播放
        self.last_time = 0         # 播放期间周期缓存的进度（停止后 getTime 会抛异常）
        self.last_stop_ts = 0      # 上次播放停止的时间戳（后台任务避开停播窗口）
        self._resume_seeked = False  # 本曲是否已自动续播 seek（避免重复）
        self.retry = None          # [pending, position, attempts]：最终回传失败后待重试
        self.retry_at = 0

    def _file_url(self):
        try:
            return self.getPlayingFile() or ''
        except Exception:
            return ''

    def onPlayBackStarted(self):
        # self.pending 可能尚未被主循环轮询加载（回调先于 5s tick 触发）→
        # 直接读盘兜底；否则直链秒开时关联不上、停止后不回传进度
        pending = self.pending or util.load_json(PENDING_FILE) or {}
        if pending and _norm_stream_url(self._file_url()) == _norm_stream_url(pending.get('stream_url')):
            self.pending = pending
            self.ours_playing = True
            util.log('检测到飞牛影视播放开始，将跟踪进度')

    def onAVStarted(self):
        """AV 管线就绪（≈首帧）时触发。

        蓝光 ISO（CDVDInputStreamBluray）+ setResolvedUrl 条目，Kodi 不应用
        ListItem 的 resumePoint（从标题 0 起播）。这里在 AV 就绪后自动 seek 到
        pending.seek_to——它来自用户在 Kodi 续播提示框/右键菜单的选择
        （resume=true→服务端位置；false→0 不 seek；未弹框默认服务端位置），
        省去"从 0 起播→用户手动拖进度条"的空跑。
        """
        if self._resume_seeked:
            return
        # pending 由主循环 5s 轮询加载，onAVStarted 触发时可能尚未就绪 → 直接读盘
        pending = self.pending or util.load_json(PENDING_FILE) or {}
        if _norm_stream_url(self._file_url()) != _norm_stream_url(pending.get('stream_url')):
            return  # 不是本插件的流
        # seek_to=0（用户选了从头开始）不 seek；旧版 pending 无此字段时回退 ts0
        seek_to = int(pending.get('seek_to', pending.get('ts0', 0)) or 0)
        if seek_to < RESUME_SEEK_THRESHOLD:
            return  # 续播点接近开头，直接从 0 看
        self._resume_seeked = True  # 先标记，防并发重复
        import threading
        threading.Thread(target=self._seek_resume, args=(seek_to,),
                         daemon=True).start()

    def _seek_resume(self, ts0):
        """onAVStarted 时播放器刚起步，稍等再 seek 更稳；失败重试一次。

        near-check：若 getTime() 已接近 ts0（Kodi 自己 resume 的非蓝光场景），
        不重复 seek。seek 最终失败则放弃（用户仍可手动 seek，不劣于现状）。
        """
        monitor = xbmc.Monitor()
        for delay in (0.8, 1.5):
            monitor.waitForAbort(delay)
            try:
                if not self.isPlaying():
                    return
                cur = self.getTime()
                if abs(cur - ts0) < 5:
                    return  # 已在续播点附近（Kodi 原生 resume），不重复 seek
                self.seekTime(ts0)
                util.log('自动续播 seek 到 %ds（起播位置 %ds）' % (ts0, cur))
                return
            except Exception as e:
                util.log('自动续播 seek 失败重试: %s' % e, xbmc.LOGWARNING)
                continue

    def onPlayBackStopped(self):
        self._resume_seeked = False
        self.last_stop_ts = time.time()
        self._report()

    def onPlayBackEnded(self):
        self._resume_seeked = False
        self.last_stop_ts = time.time()
        self._report()

    def _client(self, pending):
        """按 pending 里的 base/token 造一个客户端（播放现场的凭证快照）"""
        from resources.lib.fnapi.client import FnClient
        client = FnClient(pending.get('base', ''), verify=pending.get('verify', False))
        client.set_credentials(token=pending.get('token', ''))
        return client

    def _send(self, pending, position):
        """回传一次进度；成功则把位置写回 pending 落盘（崩溃补报据此取真值）。"""
        duration = pending.get('duration') or 0
        if duration and position > duration:
            position = duration
        try:
            self._client(pending).play_record(
                item_guid=pending.get('item_guid', ''),
                media_guid=pending.get('media_guid', ''),
                video_guid=pending.get('video_guid', ''),
                audio_guid=pending.get('audio_guid', ''),
                subtitle_guid=pending.get('subtitle_guid', ''),
                play_link=pending.get('play_link', ''),
                ts=position,
                duration=duration,
            )
        except Exception as e:
            util.log('进度回传失败: %s' % e, xbmc.LOGWARNING)
            return False
        util.log('进度已回传: %ds / %ds' % (position, duration))
        pending['ts'] = position
        util.save_json(PENDING_FILE, pending)
        return True

    def _finish(self, pending, position):
        """回传成功后的收尾：本地缓存对齐服务端 + 看完时同步服务端已看"""
        duration = pending.get('duration') or 0
        guid = pending.get('item_guid', '')
        from resources.lib import desc
        desc.note_watch(guid)
        desc.patch_progress(guid, position)
        # 播放结束可能改变服务端 watched 状态，清列表 TTL 缓存避免滞后。
        # 必须打在 service 常驻单例上：此前打在临时 client 上，只清了那个
        # 对象自己的内存缓存，常驻实例最长 300s 仍供旧列表
        util.ensure_client().invalidate_item_list_cache()
        if duration and position >= duration * 0.9:
            # 接近播完视为已看：本地原地修正（整库重建太贵），并把已看状态
            # 写回服务端——否则飞牛 App/网页仍显示"观看中"，跨端不一致
            desc.patch_watched(guid, 1)
            try:
                self._client(pending).set_watched(guid)
                util.log('服务端已标记为观看: %s' % guid[:12])
            except Exception as e:
                util.log('服务端标记已看失败: %s' % e, xbmc.LOGWARNING)

    def maybe_report(self, position):
        """播放中周期回传（主循环每 tick 调用）。

        0.7.17 及之前只在停止/播完时上报一次：看一半强退、断电、杀进程，
        整段进度全丢，服务端断点永远停在上一场。"""
        if not (self.pending and self.ours_playing) or position <= 0:
            return
        if position - _to_int(self.pending.get('ts')) < PROGRESS_REPORT_INTERVAL:
            return
        self._send(self.pending, position)

    def _report(self, position=None):
        """停止/播完时上报最终位置并收尾（播放中的上报走 maybe_report）"""
        if not (self.pending and self.ours_playing):
            return
        pending = self.pending
        self.pending, self.ours_playing = None, False

        # 停止/结束后 Kodi 已不在播放，getTime() 会抛异常（且 Kodi 会先写一条
        # error 日志即使被 except 接住）——只在仍在播放时才调 getTime，
        # 否则用主循环周期缓存的 last_time，最后才回退已上报位置/起播位置。
        if position is None:
            try:
                if self.isPlaying():
                    position = self.getTime()
            except Exception:
                position = 0
            position = (position or self.last_time
                        or _to_int(pending.get('ts')) or _to_int(pending.get('ts0')))

        if position < MIN_PROGRESS_SECONDS:
            util.delete_file(PENDING_FILE)
            util.debug('播放时间过短（%ds），不上报' % position)
            return

        if not self._send(pending, position):
            # 失败不再像 0.7.17 那样"记条 WARNING 就 finally 删 pending"：
            # 位置留在 retry 槽里由主循环重试，文件也写入本次位置（Kodi 重启
            # 后的补报据此取真值，不必等下一次周期回传）
            pending['ts'] = position
            util.save_json(PENDING_FILE, pending)
            self.retry = [pending, position, 1]
            self.retry_at = time.time() + REPORT_RETRY_S
            return
        self._finish(pending, position)
        util.delete_file(PENDING_FILE)

    def retry_report(self):
        """主循环里的失败重试（非播放、非冷却期时推进）"""
        if not self.retry or self.isPlaying() or time.time() < self.retry_at:
            return
        pending, position, attempts = self.retry
        if self._send(pending, position):
            self._finish(pending, position)
            self.retry = None
            util.delete_file(PENDING_FILE)
            return
        if attempts >= REPORT_RETRY_MAX:
            util.log('进度回传重试 %d 次仍失败，暂停（pending 保留，重启后再补报）'
                     % attempts, xbmc.LOGWARNING)
            self.retry = None
            return
        self.retry[2] = attempts + 1
        self.retry_at = time.time() + REPORT_RETRY_S


def _enrich_loop(monitor):
    """常驻预热：周期消费 enrich_queue.json，补全列表浏览时排队的详情缓存。

    放在 service 进程（而非插件 invoker）内执行——invoker 内的后台线程会让
    invoker 多活数秒，而 Kodi 播放器 OpenFile 卡在 invoker 退出上，导致
    点播首屏多卡 1-3s。挪到常驻 service 后不卡任何 invoker。
    """
    from resources.lib import meta
    cooldown = 0
    while not monitor.waitForAbort(3):
        try:
            if cooldown > 0:
                cooldown -= 1
                continue
            client = util.ensure_client()
            if client.on_token_refresh is None:
                client.on_token_refresh = lambda token: util.set_setting('token', token)
            if not client.token:
                continue   # 未配置，跳过
            done = meta.drain_enrich_queue(client)
            if done == 0:
                cooldown = 2   # 队列空，降频（~9s 轮询一次）
        except Exception as e:
            util.log('预热循环异常: %s' % e, xbmc.LOGDEBUG)
            cooldown = 4


PLAY_STOP_COOLDOWN = 150  # 播放刚停的冷却：让 Kodi 先写观看状态/刷新列表，不抢 I/O
HEAVY_IDLE_S = 300        # 重活（版本扫描/分类索引）需距上次播放停止 ≥ 5 分钟


def _prewarm_loop(monitor, player):
    """常驻整库预热：把各媒体库与"全部电影/全部剧集"范围的描述符缓存建好。

    海报墙"整库直出"（browser.filter_list）依赖 desc 范围缓存：进列表命中
    即零网络零解析，只剩 ListItem 构造。开机后台逐库构建（一次 walk 实测
    ~2s），用户进插件时通常已命中——一面墙即整库、无翻页项、零等待。
    播放中让路；desc.prewarm_scopes 每轮连建最多 3 个范围（0.7.18：此前
    每轮只建 1 个，配 30 分钟 TTL 让新片最坏要等半小时才出现）。
    播放结束后进入冷却（PLAY_STOP_COOLDOWN）：此前的实现里播放期间
    让路的全部积压任务会在停播瞬间集中释放，恰好压住 Kodi 退回列表时的
    观看状态写入与容器刷新；版本扫描等重活另要求距停播 ≥ HEAVY_IDLE_S。
    """
    from resources.lib import desc, meta
    was_playing = False
    cooldown_until = 0
    while not monitor.waitForAbort(20):
        try:
            if player.isPlaying():
                was_playing = True
                continue
            if was_playing:
                was_playing = False
                cooldown_until = time.time() + PLAY_STOP_COOLDOWN
                util.log('播放结束，后台任务冷却 %ds（避开刷列表窗口）'
                         % PLAY_STOP_COOLDOWN, xbmc.LOGDEBUG)
                continue
            if time.time() < cooldown_until:
                continue
            client = util.ensure_client()
            if client.on_token_refresh is None:
                client.on_token_refresh = lambda token: util.set_setting('token', token)
            if not client.token:
                continue   # 未配置/未登录，跳过（首次浏览登录后 token 会落盘）
            done = desc.prewarm_scopes(client)
            if done:
                util.log('整库预热完成: %s' % '、'.join(done))
                try:
                    from resources.lib import skin_sync
                    skin_sync.sync_home_widgets()
                except Exception as e:
                    util.log('同步皮肤小组件异常: %s' % e, xbmc.LOGDEBUG)
                continue
            if time.time() - player.last_stop_ts < HEAVY_IDLE_S:
                continue   # 重活等空闲窗口（last_stop_ts=0 的开机场景不受限）
            # 描述符就绪后补扫版本数（多版本"〔N版本〕"标记的数据源）
            progress = desc.scan_versions_chunk(client)
            if progress:
                util.log('版本扫描: %s' % progress)
                continue
            # 版本扫完后再刷新分类索引（流派 → 条目，本地聚合零 API）
            built = meta.build_genre_index(client)
            if built:
                util.log('分类索引已刷新: %d 个流派' % built)
                continue
            # 全部后台任务已完成，降频轮询等待新工作。5 分钟 TTL 下这个长睡
            # 直接决定新入库媒体最晚何时出现，从 240s 压到 60s；分片睡是为了
            # 用户刚看过过期墙（request_refresh 排队）时不必等满一整轮
            for _ in range(6):
                if monitor.waitForAbort(10) or desc.has_refresh_requests():
                    break
        except Exception as e:
            util.log('整库预热异常: %s' % e, xbmc.LOGDEBUG)
            monitor.waitForAbort(60)


def _check_config_prompt(monitor):
    """检测是否已配置飞牛服务器，未配置时弹窗提示配置"""
    if util.get_setting("server"):
        return
    flag_file = util.data_file("config_prompted.flag")
    if os.path.exists(flag_file):
        return

    import xbmcgui
    for _ in range(15):
        if monitor.abortRequested():
            return
        if util.get_setting("server"):
            return
        if xbmcgui.getCurrentWindowId() > 0:
            break
        time.sleep(1)

    if monitor.abortRequested() or util.get_setting("server"):
        return

    try:
        try:
            with open(flag_file, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass

        from resources.lib import skin_sync
        skin_sync.prompt_first_run_setup()
    except Exception as e:
        util.log("check_config_prompt error: %s" % e, xbmc.LOGWARNING)


def _check_skin_prompt(monitor):
    """检测并提示用户是否需要安装配套的 Arctic Fuse 3 皮肤"""
    SKIN_ID = "skin.arctic.fuse.3"
    flag_file = util.data_file("skin_prompted.flag")
    if os.path.exists(flag_file):
        return

    # 等待 Kodi GUI 界面完全就绪
    import xbmcgui
    for _ in range(15):
        if monitor.abortRequested():
            return
        if xbmc.getCondVisibility("System.HasAddon(%s)" % SKIN_ID):
            return
        if xbmcgui.getCurrentWindowId() > 0:
            break
        time.sleep(1)

    if monitor.abortRequested() or xbmc.getCondVisibility("System.HasAddon(%s)" % SKIN_ID):
        return

    try:
        try:
            with open(flag_file, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass

        dialog = xbmcgui.Dialog()
        if dialog.yesno("飞牛影视 (fntv)", "检测到尚未安装配套的 Arctic Fuse 3 皮肤，是否立即从仓库安装？"):
            xbmc.executebuiltin("InstallAddon(%s)" % SKIN_ID)
    except Exception as e:
        util.log("check_skin_prompt error: %s" % e, xbmc.LOGWARNING)


def run():
    monitor = xbmc.Monitor()
    player = FnPlayer()

    # 常驻详情预热（消费列表浏览排队）——必须在独立线程，不阻塞主循环
    import threading
    threading.Thread(target=_enrich_loop, args=(monitor,), daemon=True).start()
    # 常驻整库预热（构建海报墙描述符缓存）——同样独立线程错峰执行
    threading.Thread(target=_prewarm_loop, args=(monitor, player), daemon=True).start()
    # 提示是否配置服务器及安装配套皮肤
    threading.Thread(target=_check_config_prompt, args=(monitor,), daemon=True).start()
    threading.Thread(target=_check_skin_prompt, args=(monitor,), daemon=True).start()

    def _initial_skin_sync():
        monitor.waitForAbort(4)
        if monitor.abortRequested():
            return
        try:
            from resources.lib import skin_sync
            skin_sync.sync_home_widgets()
        except Exception as e:
            util.log("initial_skin_sync error: %s" % e, xbmc.LOGDEBUG)
    threading.Thread(target=_initial_skin_sync, daemon=True).start()

    # 启动时若发现遗留的 pending（上次 Kodi 被强退/断电），补报最后一次已知位置。
    # 取 ts（周期回传落盘的真实位置）而非 ts0（起播位置）：0.7.17 用 ts0，
    # 于是"从头开始"那场即使看了两小时也补成 0，被下面的最短时长判据静默丢弃
    stale = util.load_json(PENDING_FILE)
    if stale:
        position = _to_int(stale.get('ts')) or _to_int(stale.get('ts0'))
        util.log('发现未上报的播放记录，尝试补报（ts=%ds）' % position)
        if position < MIN_PROGRESS_SECONDS:
            util.delete_file(PENDING_FILE)
        elif player._send(stale, position):
            player._finish(stale, position)
            util.delete_file(PENDING_FILE)
        else:
            util.log('补报未成功，保留 pending 待下次启动重试', xbmc.LOGWARNING)

    # 主循环：播放中缓存进度并周期回传；空闲时推进失败重试
    while not monitor.waitForAbort(5):
        if player.isPlaying():
            # 周期缓存进度：停止后 getTime() 不可用，_report 用此缓存上报真实位置
            try:
                player.last_time = player.getTime()
            except Exception:
                pass
            # 关联 pending：回调错失时的兜底。0.3.x 起此块被错误缩进在
            # except 内（仅 getTime() 抛异常的起播窗口才执行）——代理时代
            # ISO 打开慢窗口尚存，直链秒开后窗口消失导致回传失效
            if player.pending is None:
                pending = util.load_json(PENDING_FILE)
                if pending:
                    player.pending = pending
                    if not player.last_time:
                        player.last_time = pending.get('ts0', 0) or 0
                    if _norm_stream_url(player._file_url()) == _norm_stream_url(pending.get('stream_url')):
                        player.ours_playing = True
                        util.debug('已关联播放: %s' % pending.get('stream_url'))
            player.maybe_report(player.last_time)
        else:
            player.retry_report()

    # Kodi 正在退出：还在播的先把当前位置落一次，进程一停这段就再没人回传
    if player.pending and player.ours_playing:
        util.log('Kodi 退出，回传最后播放位置')
        player._report(position=player.last_time
                       or _to_int(player.pending.get('ts')))


if __name__ == '__main__':
    run()
