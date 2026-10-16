# -*- coding: utf-8 -*-
"""整库描述符缓存——海报墙"整库直出"的数据层

真机验证（2026-09，fnOS 0.4.x）：
  POST /v/api/v1/item/list 支持 page（1 起始）/page_size（不带时默认 500
  会静默截断大库）与 ancestor_guid（服务端按库过滤，total 精确、零串库）。

设计：
  - "整库一面墙"= 每条媒体的渲染数据（label/infos/路由 url/静态海报）在
    取数时一次算好，按"范围"（哪个库 + 类型过滤 + 排序）整体落盘；
  - 进列表命中范围缓存 → 零网络、零 JSON 解析，只剩逐条构造 ListItem；
  - 富元数据（背景画/标志/演员/流派等详情类字段）不进描述符——渲染时从
    详情缓存现查（进程内 mtime 缓存后是纯字典查找），service 预热把详情
    落盘后，旧描述符无需重建即自动变富；
  - watched 状态变化走 patch_watched 原地修正（整库重建太贵）。

类型体系（真机验证）：
  Movie/Episode/Video 可播放；TV/Season 为目录（分别进 season/episode 列表）；
  其余（Directory 等）走通用 browse 兜底。
"""
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from resources.lib import util, store

TYPE_PLAYABLE = ('Movie', 'Episode', 'Video')
TYPE_TV = ('TV', 'Season')

SCOPE_TTL = 5 * 60         # 范围描述符 5 分钟；watched 由 patch_watched 主动修
# allow_expired 兜底（网络失败时陈旧总比白屏好）的可接受上限：超过即视为
# 无缓存。此前无上限，搜索/分类/继续观看这些"直接读过期缓存"的视图可以
# 一直返回几天前的数据而无人察觉。
EXPIRED_MAX_AGE = 24 * 3600
DESC_PREFIX = 'desc_'
MAX_SCOPE_FILES = 24       # 防膨胀：仅保留最近的范围文件

# 版本数探测（多版本标记）：列表接口无版本数字段（真机实测 30% 电影多版本、
# 单条 stream_list 0.1-0.2s——服务器已扫描过时全库约 1-2 分钟），由 service
# 后台逐范围扫描落盘；渲染按此打"〔N版本〕"后缀。冷文件会触发服务端
# 20s 级扫描，故带冷保护（均值超阈值即冷却中止，稍后自动续扫）。
VERSION_CACHE_FILE = 'version_cache.json'
GENRE_INDEX_FILE = 'genre_index.json'   # 与 meta.GENRE_INDEX_FILE 同名（避免循环导入）
WATCH_RECENT_FILE = 'watch_recent.json'   # guid → 最近回传进度的时刻（继续观看排序用）
REFRESH_REQUEST_FILE = 'scope_refresh.json'   # invoker → service 的"立即重建此范围"队列
VERSION_TTL = 7 * 24 * 3600
VERSION_CHUNK = 80         # 每轮扫描条数（3 并发约 15s/轮，单范围几轮扫完）
VERSION_WORKERS = 3
VERSION_COLD_ABORT_S = 3.0 # 前 6 条均值超过此值视为服务端冷扫描，冷却中止
VERSION_COOLDOWN_S = 600
_scan_cooldown = [0]


def _to_float(value):
    try:
        value = float(value)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _to_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _plugin_url(params):
    return 'plugin://%s/?%s' % (util.ADDON_ID, urlencode(params))


# ------------------------------------------------------------------ 范围管理

def scope_key(client, mdb_guid='', type_filter=''):
    """范围键：服务器 + 库 guid + 类型过滤 + 排序（排序设置变化即新范围）"""
    sort = util.get_setting('sortorder', 'ASC') or 'ASC'
    return '|'.join([client.base, mdb_guid or '', type_filter or '', sort])


def _file(key):
    return DESC_PREFIX + hashlib.md5(key.encode('utf-8')).hexdigest() + '.json'


def load_scope(key, allow_expired=False):
    """读取范围描述符；未过期返回 {'ts','title','items','progress'}，否则 None。

    allow_expired=True 时过期数据也返回，但不超过 EXPIRED_MAX_AGE（网络失败
    时陈旧总比白屏好；无上限则搜索/继续观看可能返回任意老的数据）。
    返回的是 load_json_cached 共享对象，调用方只读。
    """
    data = util.load_json_cached(_file(key))
    if not isinstance(data, dict) or not data.get('items'):
        return None
    age = time.time() - data.get('ts', 0)
    if age <= SCOPE_TTL:
        return data
    return data if (allow_expired and age <= EXPIRED_MAX_AGE) else None


def is_fresh(key):
    data = util.load_json_cached(_file(key))
    return (isinstance(data, dict) and bool(data.get('items'))
            and time.time() - data.get('ts', 0) <= SCOPE_TTL)


def store_scope(key, items, title='', progress=None):
    util.save_json(_file(key), {'ts': time.time(), 'title': title or '',
                                'items': items, 'progress': progress or []})
    try:
        import glob
        files = glob.glob(os.path.join(util.addon_data_dir(), DESC_PREFIX + '*.json'))
        if len(files) > MAX_SCOPE_FILES:
            files.sort(key=lambda p: os.path.getmtime(p))
            for p in files[:-MAX_SCOPE_FILES]:
                try:
                    os.remove(p)
                except OSError:
                    pass
    except Exception:
        pass


def scope_keys(client):
    """全部范围键（各库 + 全部电影/全部剧集），供跨范围聚合使用"""
    keys = []
    try:
        libs = client.mediadb_list() or []
    except Exception:
        libs = []
    for mdb in libs:
        if (mdb.get('category') or '').upper() == 'IPTV':
            continue
        guid = mdb.get('guid')
        if guid:
            keys.append(scope_key(client, guid, ''))
    for tf in ('Movie', 'TV'):
        keys.append(scope_key(client, '', tf))
    return keys


_stamp_memo = [0.0, '']    # (计算时刻, 结果)：detail/ 目录可能上千文件，逐次扫太贵


def invalidate_stamp():
    """清空 data_stamp 的记忆化（手动刷新后要让下一次渲染现算新戳）"""
    _stamp_memo[0] = 0.0


def data_stamp():
    """数据版本戳（分钟粒度）：描述符/版本/分类/详情缓存的最新改动时间。

    附加到目录 URL（dv= 参数）让 Kodi 的目录缓存随数据更新而失效——
    否则预热补全后 Kodi 仍展示旧容器（信息页缺演员、版本标记不出现，
    0.4.1 真机日志证实：11:34 后再无插件调用，全是 Kodi 缓存在供）。
    0.7.18 起由 _end() 统一注入到所有目录条目 URL，故每次渲染都会调用，
    加 5 秒记忆化（分钟粒度的戳本身变化很慢，重复扫盘毫无收益）。"""
    now = time.time()
    if now - _stamp_memo[0] < 5:
        return _stamp_memo[1]
    latest = 0
    base = util.addon_data_dir()
    try:
        for name in os.listdir(base):
            if name.startswith(DESC_PREFIX) or name in (VERSION_CACHE_FILE, GENRE_INDEX_FILE,
                                                         WATCH_RECENT_FILE):
                try:
                    mt = os.stat(os.path.join(base, name)).st_mtime
                    if mt > latest:
                        latest = mt
                except OSError:
                    pass
    except OSError:
        pass
    try:
        detail_dir = os.path.join(base, 'detail')
        for entry in os.scandir(detail_dir):
            try:
                mt = entry.stat().st_mtime
                if mt > latest:
                    latest = mt
            except OSError:
                pass
    except OSError:
        pass
    stamp = '%x' % int(latest // 60)
    _stamp_memo[:] = (now, stamp)
    return stamp


# ------------------------------------------------------------------ 描述符构建

def _infos_from_item(it, guessed_type=None):
    """列表字段 → Kodi video info 键值（不含详情缓存里的富字段）"""
    item_type = it.get('type') or guessed_type or ''
    is_episode = item_type in ('Episode',)
    infos = {
        'title': it.get('title', ''),
        'plot': it.get('overview', ''),
        'mediatype': 'episode' if is_episode else
                     ('tvshow' if item_type == 'TV' else 'movie'),
    }
    if it.get('tv_title'):
        infos['tvshowtitle'] = it['tv_title']
    rating = _to_float(it.get('vote_average'))
    if rating:
        infos['rating'] = rating
    if item_type == 'TV':
        infos['season'] = 0
    if is_episode or (item_type == 'Season'):
        if _to_int(it.get('season_number')):
            infos['season'] = _to_int(it['season_number'])
        if _to_int(it.get('episode_number')):
            infos['episode'] = _to_int(it['episode_number'])
    # 上映/首播日期按类型归位：电影走 premiered（Kodi 信息页显示"首映"），
    # 剧集走 aired（"首播"）——列表行电影也带 air_date，此前一律塞给
    # aired 导致电影信息页只冒出一个"首播"
    date = it.get('release_date') or it.get('air_date') or ''
    if item_type == 'Episode':
        if date:
            infos['aired'] = date
    elif date:
        infos['premiered'] = date
    if date[:4].isdigit():
        infos['year'] = int(date[:4])   # 年份直接取列表行，不依赖详情预热
    if _to_int(it.get('duration')):
        infos['duration'] = _to_int(it['duration'])
    elif _to_int(it.get('runtime')):
        infos['duration'] = _to_int(it['runtime']) * 60   # 列表行 duration 常为 0，runtime 为分钟
    if _to_int(it.get('watched')) or _to_int(it.get('is_watched')):
        infos['playcount'] = 1
    return infos


def build_item(client, it, playable=True, guessed_type=None, url=None, folder=None):
    """一条媒体记录 → 可落盘的渲染描述符（纯数据，不碰 xbmcgui）。

    url/folder 不传时按类型路由（filter/browse 的通用规则）：
      TV → tv（季列表）；可播类型 → play；其余 → browse 兜底。
    art 存服务端绝对 URL（含 ?w= 缩放），渲染时再包本地代理——构建端
    不触碰代理端口，service 进程预热也不会顺带拉起代理。
    """
    item_type = it.get('type') or guessed_type or ''
    guid = it.get('guid', '')
    if url is None:
        if item_type == 'TV':
            url = _plugin_url({'action': 'tv', 'guid': guid,
                               'title': it.get('title', '')})
            folder, playable = True, False
        elif item_type == 'Season':
            # 季 → 集列表（action=season 走 episode/list）；此前误归 action=tv
            # （season/list 对季 guid 无效），混合库里的季点进去永远为空
            url = _plugin_url({'action': 'season', 'guid': guid,
                               'title': it.get('title', '')})
            folder, playable = True, False
        elif item_type in TYPE_PLAYABLE:
            url = _plugin_url({'action': 'play', 'guid': guid})
            folder, playable = False, True
        else:
            url = _plugin_url({'action': 'browse', 'guid': guid,
                               'title': it.get('title', '')})
            folder, playable = True, False
    elif folder is None:
        folder = not playable

    title = it.get('title', '') or '未命名'
    if item_type == 'Episode':
        # 剧集标题常缺失（列表行 title 空），退回文件名剥壳
        if _to_int(it.get('episode_number')):
            title = 'E%02d %s' % (_to_int(it['episode_number']),
                                  it.get('title') or _file_label(it.get('file_name') or ''))
        elif title == '未命名' and it.get('file_name'):
            title = _file_label(it['file_name'])
    elif item_type == 'Season' and _to_int(it.get('season_number')):
        title = '%s（%d 集）' % (title, _to_int(it.get('local_number_of_episodes')))
    elif item_type == 'TV':
        # 剧集卡片标注季/集数，混合库里与电影一眼区分
        ns = _to_int(it.get('local_number_of_seasons') or it.get('number_of_seasons'))
        ne = _to_int(it.get('local_number_of_episodes') or it.get('number_of_episodes'))
        parts = '、'.join(x for x in ('%d季' % ns if ns else '', '%d集' % ne if ne else '') if x)
        if parts:
            title = '%s（%s）' % (title, parts)

    art = {}
    poster = client.image_url(it.get('poster') or it.get('posters'), width=400)
    if poster:
        art['thumb'] = art['poster'] = poster
    backdrop = client.image_url(it.get('still_path'), width=1280)
    if backdrop:
        art['fanart'] = backdrop

    return {
        'guid': guid,
        'type': item_type,
        'label': title,
        'url': url,
        'folder': bool(folder),
        'playable': bool(playable),
        'infos': _infos_from_item(it, guessed_type),
        'art': art,
        # 服务端已看位置（秒）：海报墙/继续观看据此设 Kodi resumetime。
        # 跨端一致——App/网页上看的进度也在这一字段里
        'resume_ts': _to_int(it.get('watched_ts')),
    }


def build_scope(client, mdb_guid='', type_filter='', title=''):
    """整库并行拉取 → 构建描述符 → 落盘 → 排队详情预热。返回条数。

    混合库层级收纳（真机验证：混合库 item/list 返回扁平全后代列表——
    Movie/TV/Season/Episode 混在顶层，831 条里 392 个单集直接上墙）：
    按 parent_guid 链收纳——Episode 的父 Season、祖父 TV 都在本列表时，
    Episode 与其 Season 从顶层墙隐藏（经 TV→season/list→episode/list
    导航可达）；父链闭合不了的孤儿保持顶层可播。全量 walk 实测零孤儿，
    此逻辑只为防御性兜底。
    """
    sort_type = util.get_setting('sortorder', 'ASC') or 'ASC'
    data = client.item_list_walk(ancestor_guid=mdb_guid, sort_type=sort_type)
    entries = data.get('list') or []
    # 继续观看清单：从收纳前的原始行提取（单集被收进"剧→季"后顶层墙看不
    # 到，但它们的进度必须能出现在这面墙上）
    progress = [build_item(client, e) for e in entries
                if e.get('type') in TYPE_PLAYABLE
                and _to_int(e.get('watched_ts'))
                and not (_to_int(e.get('watched')) or _to_int(e.get('is_watched')))]
    if type_filter == 'Movie':
        entries = [e for e in entries if e.get('type') == 'Movie']
    elif type_filter == 'TV':
        entries = [e for e in entries if e.get('type') == 'TV']
    elif not type_filter and mdb_guid:
        # 混合库/电视剧库的顶层墙：只留 Movie/TV/Directory/Video，
        # 收纳掉父链可闭合的 Season/Episode
        by_guid = {e.get('guid'): e for e in entries}

        def _hidden(it):
            t = it.get('type')
            if t == 'Season':
                tv = by_guid.get(it.get('parent_guid'))
                return bool(tv and tv.get('type') == 'TV')
            if t == 'Episode':
                season = by_guid.get(it.get('parent_guid'))
                if not (season and season.get('type') == 'Season'):
                    return False
                tv = by_guid.get(season.get('parent_guid'))
                return bool(tv and tv.get('type') == 'TV')
            return False

        entries = [e for e in entries if not _hidden(e)]
    items = [build_item(client, e) for e in entries]
    store_scope(scope_key(client, mdb_guid, type_filter), items, title, progress)
    try:
        from resources.lib import meta
        meta.queue_enrich([d['guid'] for d in items])
    except Exception:
        pass
    return len(items)


def scopes_to_build(client):
    """需要维护的全部范围 (mdb_guid, type_filter, 标题)：各库 + 全部电影/全部剧集"""
    todo = []
    try:
        libs = client.mediadb_list() or []
    except Exception:
        return todo
    for mdb in libs:
        if (mdb.get('category') or '').upper() == 'IPTV':
            continue    # 直播库内容不是点播媒体
        guid = mdb.get('guid')
        if guid:
            todo.append((guid, '', mdb.get('title') or '未命名库'))
    for tf, label in (('Movie', '全部电影'), ('TV', '全部剧集')):
        todo.append(('', tf, label))
    return todo


def request_refresh(key):
    """invoker 请求 service 立即重建某范围（渲染过期数据时排队，不阻塞 UI）

    5 分钟 TTL 下若仍由 invoker 同步重拉，用户每 5 分钟就要在海报墙前干等
    一次整库 walk（~2s）。改为先出旧墙、把重建交给常驻 service。"""
    try:
        keys = store.get_kv('scope_refresh')
        if keys is None:
            pending = util.load_json(REFRESH_REQUEST_FILE)
            keys = pending if isinstance(pending, list) else []
        if key not in keys:
            keys.append(key)
            store.set_kv('scope_refresh', keys)
    except Exception as e:
        util.log('request_refresh 失败: %s' % e, xbmc.LOGDEBUG)


def pop_refresh_requests():
    """取出并清空排队的范围键（service 预热优先重建这些）"""
    keys = store.get_kv('scope_refresh')
    if keys is not None:
        store.del_kv('scope_refresh')
        return keys or []
    data = util.load_json(REFRESH_REQUEST_FILE)
    keys = [k for k in data if isinstance(k, str)] if isinstance(data, list) else []
    if keys:
        util.delete_file(REFRESH_REQUEST_FILE)
    return keys


def has_refresh_requests():
    keys = store.get_kv('scope_refresh')
    if keys is not None:
        return bool(keys)
    data = util.load_json(REFRESH_REQUEST_FILE)
    return bool(isinstance(data, list) and data)



PREWARM_BATCH = 3          # 一轮预热最多重建几个范围（一次 walk 实测 ~2s）


def prewarm_scopes(client, limit=PREWARM_BATCH):
    """批量重建范围描述符（service 常驻循环调用）。返回已完成的名字列表。

    排过队的（用户刚进过、看到的是过期墙）优先，其余按库顺序补。
    0.7.17 及之前每轮只建一个：10 个范围要 10 个 tick（20s 间隔）才铺开，
    叠上 30 分钟 TTL 与空闲 240s 长睡，新片入库最坏要等半小时以上——用户
    反馈"数据总是用旧的"的主因。改为按批连建（3 个约 6s，仍不足以在停播
    窗口打突刺），配合 5 分钟 TTL 把最坏窗口压到 5 分钟 + 一轮 tick。
    """
    requested = pop_refresh_requests()
    targets = scopes_to_build(client)
    by_key = {scope_key(client, g, t): (g, t, title) for g, t, title in targets}
    priority = set(requested)
    ordered = [by_key.pop(k) for k in requested if k in by_key]
    ordered += list(by_key.values())
    done = []
    for guid, tf, title in ordered:
        if len(done) >= limit:
            break
        key = scope_key(client, guid, tf)
        # 排队项是用户刚看过过期墙的，无条件重建；其余跳过仍新鲜的
        if key not in priority and is_fresh(key):
            continue
        try:
            n = build_scope(client, mdb_guid=guid, type_filter=tf, title=title)
        except Exception as e:
            util.log('整库预热失败 %s: %s' % (title, e))
            continue
        done.append('%s（%d 条）' % (title, n))
    return done


# ------------------------------------------------------------------ 版本数探测

def load_versions():
    """guid → {'ts', 'n'(版本数), 'v'(各版本标签列表)} 全量映射。

    'v' 为 0.4.3 新增：信息页剧情里列出各版本画质/编码明细；
    旧条目缺 'v' 会被 _version_targets 视为待重扫自动补齐。"""
    data = util.load_json_cached(VERSION_CACHE_FILE)
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ 继续观看

WATCH_RECENT_MAX = 500


def note_watch(guid):
    """记录"这个条目刚看过"的时刻（service 进度回传时调用）。

    服务端列表行只有 watched_ts（已看位置秒数），没有最近观看时间，
    继续观看墙无从按"最近在看的"排序。本地这份只覆盖 Kodi 端观看，
    与服务端全量成员取并集后充当排序主键。"""
    if not guid:
        return
    data = store.get_kv('watch_recent')
    if data is None:
        data = util.load_json(WATCH_RECENT_FILE)
    data = data if isinstance(data, dict) else {}
    data[guid] = int(time.time())
    if len(data) > WATCH_RECENT_MAX:   # 防膨胀：只留最近 500 条
        data = dict(sorted(data.items(), key=lambda kv: kv[1])[-WATCH_RECENT_MAX:])
    store.set_kv('watch_recent', data)


def load_progress(client):
    """跨范围聚合"有进度且未看完"的条目（guid 去重），最近在看的在前。

    优先直接调用服务端继续观看接口（保留服务端顺序，跨端一致，
    App/网页上看的也参与排序）；接口不可用时回退到本地缓存 +
    (本地时间戳, resume_ts) 排序。
    """
    # 1. 优先：直接调用服务端继续观看接口，保留服务端排序（GET /v/api/v1/play/list）
    try:
        raw_list = client.keep_watching_list()
        if isinstance(raw_list, list):
            out = []
            seen = set()
            for it in raw_list:
                g = it.get('guid', '')
                if not g or g in seen:
                    continue
                seen.add(g)
                # play/list API 用 ts 表示已看位置（秒），build_item 读 watched_ts
                if 'ts' in it and 'watched_ts' not in it:
                    it = dict(it)
                    it['watched_ts'] = it['ts']
                d = build_item(client, it)
                if d:
                    out.append(d)
            return out
    except Exception as e:
        util.debug('keep_watching_list 调用失败，回退本地缓存: %s' % e)

    # 2. 回退：本地缓存合并 + 排序（无服务端接口时的兜底）
    recent = store.get_kv('watch_recent')
    if recent is None:
        recent = util.load_json(WATCH_RECENT_FILE)
    recent = recent if isinstance(recent, dict) else {}
    merged = {}
    for key in scope_keys(client):
        scope = load_scope(key, allow_expired=True) or {}
        for d in scope.get('progress') or []:
            g = d.get('guid', '')
            if g and g not in merged:
                merged[g] = d
    out = list(merged.values())
    out.sort(key=lambda d: (recent.get(d.get('guid', '')) or 0,
                            d.get('resume_ts') or 0), reverse=True)
    return out



_FILE_EXTS = ('.strm', '.m2ts', '.mkv', '.iso', '.mp4', '.avi', '.wmv', '.ts')


def _file_label(name):
    """文件名 → 版本标签：剥掉 .strm/.iso 等扩展名壳，保留原始命名
    （网盘 strm 条目的 video_streams 无元数据，版本信息只在文件名里）"""
    base = (name or '').rsplit('/', 1)[-1].strip()
    changed = True
    while changed:
        changed = False
        low = base.lower()
        for ext in _FILE_EXTS:
            if low.endswith(ext) and len(base) > len(ext):
                base = base[:-len(ext)].strip()
                changed = True
                break
    return (base or '版本')[:80]


def _version_label(stream):
    """video_stream → 紧凑版本标签（兜底：文件名缺失时用流字段）"""
    parts = []
    if stream.get('title'):
        parts.append(str(stream['title']))
    res = stream.get('resolution_type') or ''
    if not res and stream.get('width') and stream.get('height'):
        res = '%dx%d' % (stream['width'], stream['height'])
    if res:
        parts.append(str(res))
    if stream.get('codec_name'):
        parts.append(str(stream['codec_name']).upper())
    if stream.get('is_bluray'):
        parts.append('蓝光原盘')
    return ' | '.join(parts) if parts else '版本'


def _version_targets(items):
    """范围内需要探测的 (全部可探测数, 未探测/标签是占位符 guid 列表)——仅电影/其他视频"""
    cache = util.load_json(VERSION_CACHE_FILE) or {}
    now = time.time()
    total, todo = 0, []
    for d in items:
        if d.get('folder') or d.get('type') not in ('Movie', 'Video'):
            continue
        total += 1
        entry = cache.get(d.get('guid', ''))
        labels = [v for v in (entry or {}).get('v') or [] if v and v != '版本']
        if (not entry or now - entry.get('ts', 0) > VERSION_TTL
                or not entry.get('v') or not labels):
            todo.append(d.get('guid', ''))
    return total, todo


def scan_versions_chunk(client):
    """挑一个范围探测 ≤VERSION_CHUNK 个条目的版本数（service 常驻调用）。

    版本数只能逐条 GET /stream/list/{guid}（先串行试 6 条做冷保护：均值
    超阈值视为服务端要现扫描，冷却 10 分钟后再续，防触发全库扫描风暴）。
    返回进度描述（如 '115-Strm 160/1256'），无需扫描返回 None。
    """
    if time.time() < _scan_cooldown[0]:
        return None
    try:
        libs = client.mediadb_list() or []
    except Exception:
        return None
    for mdb in libs:
        if (mdb.get('category') or '').upper() == 'IPTV':
            continue
        guid = mdb.get('guid')
        if not guid:
            continue
        key = scope_key(client, guid, '')
        if not is_fresh(key):
            continue    # 描述符未就绪，等整库预热完成
        scope = load_scope(key) or {}
        total, todo = _version_targets(scope.get('items') or [])
        if not todo:
            continue
        title = scope.get('title') or mdb.get('title') or guid[:12]

        # 冷保护：先串行试 6 条
        probe = todo[:6]
        t0 = time.time()
        cache = util.load_json(VERSION_CACHE_FILE) or {}

        def _probe_one(g):
            sl = client.stream_list(g)
            files = sl.get('files') or []
            streams = sl.get('video_streams') or []
            # 版本信息在文件名里（网盘 strm 的 video_streams 无元数据）
            if files:
                labels = [_file_label(f.get('file_name') or '') for f in files[:8]]
            else:
                labels = [_version_label(s) for s in streams[:8]]
            cache[g] = {'ts': time.time(),
                        'n': max(len(streams), len(files), 1),
                        'v': labels}

        try:
            for g in probe:
                _probe_one(g)
        except Exception as e:
            _scan_cooldown[0] = time.time() + VERSION_COOLDOWN_S
            util.log('版本扫描遇冷（服务端需现扫描），冷却后重试: %s' % str(e)[:60])
            util.save_json(VERSION_CACHE_FILE, cache)
            return None
        if len(probe) >= 3 and (time.time() - t0) / len(probe) > VERSION_COLD_ABORT_S:
            _scan_cooldown[0] = time.time() + VERSION_COOLDOWN_S
            util.log('版本扫描过慢（均值 %.1fs/条），冷却后重试' % ((time.time() - t0) / len(probe)))
            util.save_json(VERSION_CACHE_FILE, cache)
            return None

        rest = todo[len(probe):VERSION_CHUNK]

        def _one(g):
            try:
                _probe_one(g)
            except Exception:
                # 失败按单版本记录（无标签），避免反复重试卡住扫描
                cache[g] = {'ts': time.time(), 'n': 1, 'v': []}

        with ThreadPoolExecutor(max_workers=VERSION_WORKERS) as pool:
            list(pool.map(_one, rest))
        util.save_json(VERSION_CACHE_FILE, cache)
        return '%s %d/%d' % (title, total - len(todo) + len(probe) + len(rest), total)
    return None


# ------------------------------------------------------------------ watched 修正

def patch_watched(guid, playcount=1):
    """把某条目的观看状态原地写进所有命中的范围文件（整库重建太贵）。

    playcount=0 同样支持——服务端取消已看后本地要对齐，否则 Kodi 会一直
    显示已看直到 TTL 重建（0.7.17 只写 1、从不写 0，是单向的）。
    同时维护 progress 清单：看完的条目必须从"继续观看"墙上摘掉，取消已看
    时无从恢复秒数（服务端字段要重建才拿得到），保持摘除等下次重建补回。
    save_json 更新 mtime，渲染端的 load_json_cached 下次读取自动重载。
    """
    if not guid:
        return
    try:
        import glob
        for path in glob.glob(os.path.join(util.addon_data_dir(),
                                           DESC_PREFIX + '*.json')):
            name = os.path.basename(path)
            data = util.load_json(name)
            if not isinstance(data, dict):
                continue
            changed = False
            for it in data.get('items') or []:
                if it.get('guid') == guid:
                    infos = it.setdefault('infos', {})
                    if playcount:
                        if infos.get('playcount') != playcount:
                            infos['playcount'] = playcount
                            changed = True
                    elif infos.pop('playcount', None) is not None:
                        changed = True
            prog = data.get('progress') or []
            if playcount and prog:
                kept = [p for p in prog if p.get('guid') != guid]
                if len(kept) != len(prog):
                    data['progress'] = kept
                    changed = True
            if changed:
                util.save_json(name, data)
    except Exception as e:
        util.log('patch_watched 失败: %s' % e)


def patch_progress(guid, resume_ts):
    """原地刷新某条目的续播点（秒），供"继续观看"墙与海报墙即时反映进度。

    进度每 60 秒回传一次，为一个字段重建整库（walk ~2s）不划算；这里只改
    描述符里的 resume_ts。条目不在 progress 清单里（此前无进度）时不新增
    ——新增需要完整的渲染数据，交给 TTL 重建自然收编。
    """
    if not guid or resume_ts <= 0:
        return
    try:
        import glob
        for path in glob.glob(os.path.join(util.addon_data_dir(),
                                           DESC_PREFIX + '*.json')):
            name = os.path.basename(path)
            data = util.load_json(name)
            if not isinstance(data, dict):
                continue
            changed = False
            for bucket in ('items', 'progress'):
                for it in data.get(bucket) or []:
                    if it.get('guid') == guid and it.get('resume_ts') != resume_ts:
                        it['resume_ts'] = resume_ts
                        changed = True
            if changed:
                util.save_json(name, data)
    except Exception as e:
        util.log('patch_progress 失败: %s' % e)
