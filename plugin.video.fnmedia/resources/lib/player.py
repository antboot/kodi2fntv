# -*- coding: utf-8 -*-
"""播放：解析流地址、蓝光多版本选择、外挂字幕、断点续播、进度回传准备"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, unquote, quote

import xbmc
import xbmcgui
import xbmcplugin

from resources.lib import util, proxy, sniff
from resources.lib.browser import plugin_url
from resources.lib.fnapi.client import ApiError, CLOUD_LINK_CACHE_FILE

PENDING_FILE = 'pending.json'
STREAM_CACHE_FILE = 'stream_list_cache.json'
STREAM_CACHE_TTL = 6 * 3600  # 6 小时（避免每次点击都触发服务端"获取文件视频信息"扫描）


def _load_stream_cache():
    data = util.load_json(STREAM_CACHE_FILE) or {}
    return data if isinstance(data, dict) else {}


def _get_cached_stream_list(item_guid):
    """读取落盘的 stream/list 缓存（未过期才返回）

    play 热路径：走 mtime 校验的进程内缓存（util.load_json_cached），
    同一进程内仅首次读盘解析；写路径 _store_stream_list 仍用 load_json
    取独立副本做读改写，避免污染共享缓存。"""
    import time as _time
    cache = util.load_json_cached(STREAM_CACHE_FILE)
    entry = cache.get(item_guid) if isinstance(cache, dict) else None
    if not entry:
        return None
    if _time.time() - entry.get('ts', 0) > STREAM_CACHE_TTL:
        return None
    return entry.get('data') or {}


def _store_stream_list(item_guid, data):
    """写入落盘缓存（后台线程调用，失败静默）"""
    import time as _time
    cache = _load_stream_cache()
    cache[item_guid] = {'ts': _time.time(), 'data': data}
    # 只保留最近 200 条，防止无限膨胀
    if len(cache) > 200:
        ordered = sorted(cache.items(), key=lambda kv: kv[1].get('ts', 0))
        cache = dict(ordered[-200:])
    util.save_json(STREAM_CACHE_FILE, cache)


def _drop_stream_cache(item_guid):
    """作废单个条目的 stream/list 缓存（「刷新字幕并播放」用）"""
    cache = _load_stream_cache()
    if cache.pop(item_guid, None) is not None:
        util.save_json(STREAM_CACHE_FILE, cache)


def _prepare():
    """准备客户端：复用已保存 token，回调前置，不再发 user/info 校验往返。

    每次插件调用都是新进程，ensure_token 会对已保存 token 仍发一次
    GET /user/info 校验（~115ms/次）。但 client.call() 自带 401/403
    自动重登+重试，与该校验等价，故省掉以加速首屏。token 过期时由
    play_info 的 call() 懒触发重登（_save_token 持久化+回调）。
    """
    client = util.ensure_client()
    # 回调必须在任何 API 调用前就位：否则 token 过期重登时新 token 不会持久化
    client.on_token_refresh = lambda token: util.set_setting('token', token)
    client.on_base_change = lambda base: util.set_setting('server', base)
    if not client.token and not client._can_relogin():
        raise ApiError('未配置账号密码，且无可用令牌，请先在插件设置中配置')
    proxy.set_client(client)
    return client


def _to_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _clean_file_name(name):
    """整理真实文件名：取 basename；去掉 .strm 外壳。

    云盘 .strm 文件本身只是几百字节的 URL 文本，内容才是视频——
    xxx.iso.strm 必须以 .iso 结尾进播放 URL，Kodi 才会按光盘镜像
    （UDF/BDMV）挂载；ffmpeg 直接探测裸 ISO 开头的 32KB 全零区必失败。"""
    if not name:
        return ''
    name = str(name).rstrip('/').rsplit('/', 1)[-1].strip()
    if name.lower().endswith('.strm'):
        name = name[:-5].strip()
    return name


def _pick_file_name(stream_list, media_guid):
    """按优先级解析真实文件名：stream_list.files（guid 匹配）→ 云链缓存 file_stream

    云盘 strm 的文件名剥掉 .strm 壳后没有真实容器后缀（如 "…7.1-WF"），
    而播放 URL 必须以真实后缀结尾——Kodi 靠它识别容器/挂载光盘镜像，
    代理也靠它在上游只给 octet-stream 时补 Content-Type（真机坑⑦⑮）。
    真实后缀取自云链缓存：direct_link URL 尾部文件名优先（…/n/xxx.mkv），
    服务端探测的容器类型 wrapper 兜底（MKV→.mkv）。"""
    name = ''
    for f in ((stream_list or {}).get('files') or []):
        if f.get('guid') == media_guid:
            name = _clean_file_name(f.get('file_name'))
            if name:
                break
    try:
        disk = util.load_json_cached(CLOUD_LINK_CACHE_FILE)
        cloud_data = (disk.get(media_guid) or {}).get('data') or {}
        if not name:
            name = _clean_file_name((cloud_data.get('file_stream') or {}).get('file_name'))
        if name and os.path.splitext(name)[1].lower() not in proxy.MIME_BY_EXT:
            ext = _guess_container_ext(cloud_data)
            if ext:
                name += ext
        return name
    except Exception:
        return name


def _guess_container_ext(cloud_data):
    """从云链缓存推断真实容器后缀：直链 URL 尾部文件名优先，wrapper 兜底"""
    for q in ((cloud_data or {}).get('direct_link_qualities') or []):
        url = q.get('url') or ''
        if url.startswith('http'):
            tail = unquote(urlparse(url).path.rsplit('/', 1)[-1])
            ext = os.path.splitext(tail)[1].lower()
            if ext in proxy.MIME_BY_EXT:
                return ext
    wrapper = str(((cloud_data or {}).get('video_stream') or {}).get('wrapper') or '').strip().upper()
    return {
        'MKV': '.mkv', 'MP4': '.mp4', 'MPEGTS': '.ts', 'MPEG-TS': '.ts',
        'AVI': '.avi', 'WMV': '.wmv', 'QUICKTIME': '.mov', 'FLV': '.flv',
        'WEBM': '.webm', 'MPEG': '.mpg', 'MPEG4': '.mp4', 'M2TS': '.m2ts',
    }.get(wrapper, '')


def _build_vfs_url(playable, file_name):
    """构造 VFS 插件播放 URL（ISO/BDMV 直链交给 VFS 注入头取流）。

    URL 形如 <scheme>://play/<文件名>.iso?d=<base64url(描述符)>——扩展名必须在
    path 尾（Kodi 按扩展名识别光盘镜像，放在 ? 参数里无效，插件机制限制无法
    绕过）。描述符为自包含 JSON（'u'=直链，'h'=直连所需请求头 UA/Cookie，
    'x'=过期时间戳 0 未知），VFS 插件按描述符取流，需自行实现请求头注入、
    每链接并发 ≤2（115 风控）与精确 Range/Seek/Stat。设置 VFS Scheme 留空
    （默认，未实装）时返回 None，ISO 回退本地代理。"""
    scheme = (util.get_setting('vfsscheme', '') or '').strip().rstrip(':/')
    if not scheme:
        return None
    desc = {'u': playable['base'], 'x': playable.get('expire') or 0}
    if playable.get('headers'):
        desc['h'] = playable['headers']
    try:
        payload = util.b64url_encode(
            json.dumps(desc, separators=(',', ':')).encode('utf-8'))
    except Exception as e:
        util.log('VFS 描述符编码失败: %s' % e, xbmc.LOGWARNING)
        return None
    name = quote(file_name or 'video.iso', safe='')
    return '%s://play/%s?d=%s' % (scheme, name, payload)


# fnOS 字幕 language 字段 → Kodi 可识别的 ISO 639-2B 码
_SUB_LANG_CODES = {
    'zh': 'chi', 'chi': 'chi', 'zho': 'chi', 'chs': 'chi', 'cht': 'chi',
    'zh-cn': 'chi', 'zh-tw': 'chi', 'zh-hans': 'chi', 'zh-hant': 'chi',
    'cn': 'chi', 'sc': 'chi', 'tc': 'chi',
    'en': 'eng', 'eng': 'eng',
    'ja': 'jpn', 'jpn': 'jpn', 'jp': 'jpn',
    'ko': 'kor', 'kor': 'kor',
    'fr': 'fre', 'fra': 'fre',
    'de': 'ger', 'deu': 'ger',
    'es': 'spa', 'spa': 'spa',
    'ru': 'rus', 'rus': 'rus',
    'it': 'ita', 'ita': 'ita',
    'pt': 'por', 'por': 'por',
    'ar': 'ara', 'ara': 'ara',
    'th': 'tha', 'tha': 'tha',
    'vi': 'vie', 'vie': 'vie',
    'ms': 'may', 'msa': 'may',
    'id': 'ind', 'ind': 'ind',
    'hi': 'hin', 'hin': 'hin',
    'tr': 'tur', 'tur': 'tur',
    'pl': 'pol', 'pol': 'pol',
    'nl': 'dut', 'nld': 'dut',
    'sv': 'swe', 'swe': 'swe',
    'da': 'dan', 'dan': 'dan',
    'fi': 'fin', 'fin': 'fin',
    'no': 'nor', 'nor': 'nor',
    'el': 'gre', 'ell': 'gre',
    'he': 'heb', 'heb': 'heb',
    'cs': 'cze', 'ces': 'cze',
    'hu': 'hun', 'hun': 'hun',
    'uk': 'ukr', 'ukr': 'ukr',
    'fa': 'per', 'fas': 'per',
}


def _sub_language(sub):
    """fnOS 的 language 字段转 Kodi 语言码；未知返回空串（宁可不写码也不休假语言）"""
    code = (sub.get('language') or '').strip().lower()
    if not code or 'unknow' in code or code in ('und', 'unknown'):
        return ''
    mapped = _SUB_LANG_CODES.get(code)
    if mapped:
        return mapped
    return code if len(code) in (2, 3) and code.isalpha() else ''


_SUB_FORMATS = {'srt', 'ass', 'vtt', 'sup'}

# 目录内属于"本插件落盘的字幕"，刷新时据此清理不再存在的轨道
_SUB_EXTS = ('.srt', '.ass', '.vtt', '.sup')
_SUB_META = 'subs.meta.json'


def _sub_format(sub):
    """字幕容器后缀：优先 format，缺失时按 codec 推断；无法确定返回空串。

    旧实现把不认识的格式一律当 srt 落盘，PGS 位图字节会被存成 .srt，
    Kodi 按文本解析必然失败。位图字幕只有 PGS（.sup）能作为外挂轨播放，
    DVD VobSub 等直接跳过。"""
    fmt = (sub.get('format') or '').strip().lower().lstrip('.')
    if fmt == 'webvtt':
        fmt = 'vtt'
    elif fmt == 'ssa':
        fmt = 'ass'   # SSA v4 与 ASS 同容器；且 "ssa" 是 Kodi 语言表里的 Nilo-Saharan
    elif fmt in ('pgs', 'hdmv_pgs'):
        fmt = 'sup'
    if fmt in _SUB_FORMATS:
        return fmt
    codec = (sub.get('codec_name') or '').strip().lower()
    if codec in ('ass', 'ssa'):
        return 'ass'
    if codec in ('pgs', 'hdmv_pgs_subtitle', 'sup'):
        return 'sup'
    if 'webvtt' in codec or codec == 'vtt':
        return 'vtt'
    if codec in ('subrip', 'srt', 'mov_text', 'text'):
        return 'srt'
    return ''


def _sub_title(sub):
    """字幕标题整理成文件名 token：去掉扩展名与文件名非法字符"""
    title = (sub.get('title') or '').strip()
    lower = title.lower()
    for ext in ('.srt', '.ass', '.ssa', '.vtt', '.sup'):
        if lower.endswith(ext):
            title = title[:-len(ext)].strip()
            break
    title = ''.join(' ' if (c in '/\\:*?"<>|@' or not c.isprintable()) else c
                    for c in title)
    return ' '.join(title.split())[:60]


def _sub_signature(sub):
    """字幕版本指纹：任一字段变化即视为需要重下"""
    return '%s|%s|%s' % (sub.get('update_time') or '', sub.get('file_size') or '',
                         sub.get('guid') or '')


def _load_sub_meta(tmp):
    try:
        with open(os.path.join(tmp, _SUB_META), 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_sub_meta(tmp, meta):
    try:
        path = os.path.join(tmp, _SUB_META)
        part = path + '.part'
        with open(part, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(part, path)
    except Exception as e:
        util.debug('字幕元数据写入失败: %s' % e)


def _download_one(client, guid, path, fmt):
    """下载单条字幕并原子落盘；返回是否成功。

    PGS 位图字节必须以 PG 魔数开头，否则说明服务端给的不是外挂字幕
    （或格式标错），宁可不挂这一轨也不给 Kodi 一个必然解析失败的文件。"""
    content = client.download_subtitle(guid)
    if not content:
        return False
    if fmt == 'sup' and not content.startswith(b'PG'):
        util.debug('外挂 PGS 字幕字节非法（缺 PG 魔数），跳过: %s' % guid)
        return False
    part = path + '.part'
    with open(part, 'wb') as f:
        f.write(content)
    os.replace(part, path)
    return True


def _download_subtitles(client, stream_list, media_guid='', force=False):
    """下载外挂字幕到本地临时目录，返回文件路径列表

    Kodi 的外挂字幕语言/名称完全由文件名解析
    （CUtil::GetExternalStreamDetailsFromFilename）：从右往左取第一个可识别的
    语言码作为语言，其余 token 拼成名称；没有语言码一律显示 UNK。此前用
    `<title>@<guid8>` 命名，于是三条中文轨在 OSD 里显示成
    「UNK | chinese@9d41b34a」。现在按「格式 + 标题 token + ISO 语言码」命名，
    并按 media_guid 分目录，避免不同视频的同名轨道互相覆盖。

    并发与缓存：目录内 subs.meta.json 记录每条字幕的版本指纹
    （update_time|file_size|guid），指纹不变直接用本地文件；缺失项最多 3 线程
    并行下载（115 等网盘对单链路并发敏感，不开更大）。刷新时清理已不存在的
    轨道文件，避免 OSD 里残留旧字幕。"""
    streams = (stream_list.get('subtitle_streams') or [])
    external = [s for s in streams if _to_int(s.get('is_external')) == 1 and s.get('guid')]
    if not external:
        return []

    tmp = os.path.join(util.temp_dir(), (media_guid or 'misc')[:16])
    try:
        os.makedirs(tmp, exist_ok=True)
    except Exception:
        pass

    # 先按流顺序算定文件名，再下载：下载是乱序并发的，最后仍按 Kodi 的
    # 轨道顺序返回，保证 OSD 里的排列与服务端一致。
    targets = []
    used = {}
    for sub in external:
        fmt = _sub_format(sub)
        if not fmt:
            util.debug('跳过不支持的外挂字幕格式: %s %s' % (
                sub.get('format'), sub.get('codec_name')))
            continue
        lang = _sub_language(sub)
        title = _sub_title(sub)
        # 格式 token 放最前：Kodi JSON-RPC 的字幕流只有 language/name，
        # 没有 codec/格式字段（皮肤的字幕面板取不到），写进名称才能显示
        # 「中文 | ASS chinese 外挂」这样的可读行。
        tokens = [fmt.upper()]
        if title:
            tokens.append(title)
        key = (lang, title.lower())
        used[key] = used.get(key, 0) + 1
        if used[key] > 1:
            tokens.append('Track %d' % used[key])
        if lang:
            tokens.append(lang)   # 语言码必须在最后：Kodi 从右往左扫描
        targets.append((sub, os.path.join(tmp, '%s.%s' % ('.'.join(tokens), fmt)), fmt))

    meta = _load_sub_meta(tmp)
    new_meta = {}
    missing = []
    for sub, path, fmt in targets:
        base = os.path.basename(path)
        sig = _sub_signature(sub)
        new_meta[base] = sig
        if not force and os.path.isfile(path) and meta.get(base) == sig:
            continue
        missing.append((sub, path, fmt))

    results = {}
    if missing:
        with ThreadPoolExecutor(max_workers=min(3, len(missing))) as pool:
            futures = {
                pool.submit(_download_one, client, sub['guid'], path, fmt): path
                for sub, path, fmt in missing
            }
            for fut, path in futures.items():
                base = os.path.basename(path)
                try:
                    results[base] = bool(fut.result())
                except Exception as e:
                    util.log('字幕下载失败 %s: %s' % (base, e), xbmc.LOGWARNING)
                    results[base] = False

    paths = []
    for sub, path, fmt in targets:
        base = os.path.basename(path)
        if results.get(base) is False:
            # 下载失败但目录里有旧文件：继续用旧的（比没有字幕好）
            if os.path.isfile(path):
                new_meta[base] = meta.get(base, '')
                paths.append(path)
            else:
                new_meta.pop(base, None)
            continue
        if os.path.isfile(path):
            paths.append(path)

    for name in os.listdir(tmp):
        if name in new_meta or name == _SUB_META or name.endswith('.part'):
            continue
        if name.lower().endswith(_SUB_EXTS):
            try:
                os.remove(os.path.join(tmp, name))
            except Exception:
                pass
    _save_sub_meta(tmp, new_meta)
    return paths


def play(handle, params):
    """解析并播放一个条目

    关键路径精简（setResolvedUrl 尽早返回，让转圈尽快出现）：
    - play_info（必需，拿 media_guid + 断点 + 标题）
    - 海报/背景图/标志 + 基础元数据（全部来自 play_info，零额外 API）
    - 详情/演员表：仅读缓存（即时）；未命中后台预热（下次满配）
    - 演员表装配（build_cast 最重）一律移到 setResolvedUrl 之后后台执行
    - 文件名取不到时限等 stream_list 最多 1 秒（云盘 ISO 走云链缓存即时取）
    """
    guid = params.get('guid', '')
    source_index = _to_int(params.get('source', 0))
    client = _prepare()

    try:
        play_info = client.play_info(guid)
    except ApiError as e:
        util.log('play_info 失败: %s' % e.message, xbmc.LOGERROR)
        util.notify('获取播放信息失败：%s' % e.message, error=True)
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem(offscreen=True))
        return

    item = play_info.get('item') or {}
    label = item.get('title') or guid
    util.log('play_info ok: %s' % label)

    # stream/list：优先落盘缓存（避免服务端重复扫描）；无缓存时后台补抓。
    # 已知云盘的条目（云链缓存里有它的 media_guid）不再补抓：云盘解析
    # 会触发服务端"获取文件视频信息"扫描，且云盘项的多版本/字幕信息
    # 通常为空，文件名也能从云链缓存取得。
    cloud_known = (play_info.get('media_guid') or '') in util.load_json_cached(CLOUD_LINK_CACHE_FILE)
    refresh_subs = (params.get('refresh') or '') == '1'
    stream_list = _get_cached_stream_list(guid) or {}
    if refresh_subs:
        # 「刷新字幕并播放」：作废旧缓存并同步重拉一次（后台补抓赶不上本次起播）
        _drop_stream_cache(guid)
        try:
            stream_list = client.stream_list(guid, timeout=15) or {}
            if stream_list:
                _store_stream_list(guid, stream_list)
        except Exception as e:
            util.log('刷新字幕拉取流列表失败，沿用旧缓存: %s' % e, xbmc.LOGWARNING)
    fetch_event = None

    # 字幕清单新鲜度：视频流缓存 6 小时，但 NAS 上的字幕随时会增删。对已有
    # 缓存的条目每次播放都并发重拉一次 stream/list（已扫描过的条目实测 ~0.1s），
    # 起播前最多等 2.5s 取新清单——拿不到就沿用缓存，不拖慢起播。
    # 注意不能按 cloud_known 跳过：cloud_link_cache 只表示"换过链并缓存了直链"，
    # 本地/NAS 影片只要解析过一次直链也会命中，跳过会让这类影片的字幕永远不刷新
    # （真机踩坑：Super Mario 因此一条 stream/list 都没发）。
    subs_box = {}
    subs_event = None
    if stream_list and not refresh_subs:
        import threading
        subs_event = threading.Event()

        def _refresh_subs():
            import time as _time
            t0 = _time.time()
            try:
                data = client.stream_list(guid, timeout=20)
                if data:
                    subs_box['data'] = data
                    subs_box['cost'] = _time.time() - t0
                    _store_stream_list(guid, data)
            except Exception as e:
                util.log('字幕清单刷新失败（沿用缓存）: %s' % e, xbmc.LOGWARNING)
            finally:
                subs_event.set()

        threading.Thread(target=_refresh_subs, daemon=True).start()

    if not stream_list and source_index == 0 and not cloud_known:
        import threading
        fetch_event = threading.Event()

        def _fetch_and_cache():
            try:
                data = client.stream_list(guid)
                if data:
                    _store_stream_list(guid, data)
            except Exception as e:
                util.log('stream_list 后台补抓失败: %s' % e, xbmc.LOGWARNING)
            finally:
                fetch_event.set()

        threading.Thread(target=_fetch_and_cache, daemon=True).start()
    elif not stream_list and source_index > 0:
        # 选版本必须同步等待完整列表
        try:
            stream_list = client.stream_list(guid)
            if stream_list:
                _store_stream_list(guid, stream_list)
        except ApiError as e:
            util.log('stream_list 失败: %s' % e.message, xbmc.LOGERROR)

    # 多版本询问需要流列表；云盘快速通道默认不取（避免触发服务端扫描）。
    # 版本扫描缓存（service 后台维护）若已标记该影片多版本，按 5 秒预算
    # 同步补取一次：拿到则弹版本选择，超时/失败按默认版本直接起播。
    if not stream_list and source_index == 0 \
            and util.get_setting('askversion', 'true') == 'true':
        try:
            from resources.lib import desc
            known_versions = (desc.load_versions().get(guid) or {}).get('n') or 0
        except Exception:
            known_versions = 0
        if known_versions > 1:
            util.log('多版本影片（%d 版本）预取流列表用于版本询问' % known_versions)
            try:
                stream_list = client.stream_list(guid, timeout=5) or {}
                if stream_list:
                    _store_stream_list(guid, stream_list)
            except Exception as e:
                util.log('预取流列表超时/失败，按默认版本起播: %s' % e, xbmc.LOGWARNING)

    streams = stream_list.get('video_streams') or []
    chosen = None
    if streams and source_index == 0 and len(streams) > 1 \
            and util.get_setting('askversion', 'true') == 'true':
        # 多版本影片起播前询问版本（设置可关）。流列表未就绪时最多等 3 秒——
        # 服务端已扫描的条目百毫秒级即回，冷文件超时则按默认版本直接起播
        # （仍可右键"选择版本播放"补选）
        if fetch_event is not None:
            fetch_event.wait(3.0)
            stream_list = _get_cached_stream_list(guid) or stream_list
            streams = stream_list.get('video_streams') or streams
        if len(streams) > 1:
            xbmc.executebuiltin('Dialog.Close(busydialog)')
            # 标签优先用原始文件名（网盘 strm 的 video_streams 无元数据，
            # 只有文件名里才有版本信息），NAS 本地文件再退回流字段
            files = stream_list.get('files') or []
            labels = []
            if len(files) == len(streams):
                from resources.lib import desc as _desc
                labels = [_desc._file_label(f.get('file_name') or '')
                          for f in files]
            if not any(labels):
                labels = _version_labels(streams)
            util.log('多版本影片询问版本: 共 %d 个' % len(streams))
            choice = xbmcgui.Dialog().select('选择版本（共 %d 个，取消播默认）' % len(streams),
                                             labels)
            if 0 <= choice < len(streams):
                source_index = choice
                util.log('用户选择版本 %d: %s' % (choice + 1, labels[choice]))
    if streams:
        # 选择版本（蓝光原盘多版本对应多个 video_stream）
        if 0 <= source_index < len(streams):
            chosen = streams[source_index]
        else:
            chosen = streams[0]
        media_guid = chosen.get('media_guid')
    else:
        # 快速通道兜底：play_info 自带的 media_guid
        media_guid = play_info.get('media_guid')

    if not media_guid:
        util.notify('没有可播放的视频流（文件可能未完成入库）', error=True)
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem(offscreen=True))
        return

    # 播放 URL 尾部带真实文件名（含后缀）：代理路径需要它让播放器识别容器类型、
    # 代理补 Content-Type；云盘 strm/ISO 必须剥 .strm 壳以真实后缀结尾。
    # 直链路径不把文件名拼进 URL（直链自带后缀），但 file_name 仍用于日志/兜底。
    file_name = _pick_file_name(stream_list, media_guid)
    if not file_name and fetch_event is not None:
        fetch_event.wait(1.0)
        stream_list = _get_cached_stream_list(guid) or stream_list
        file_name = _pick_file_name(stream_list, media_guid)
        if file_name:
            util.debug('等待 stream_list 取到文件名: %s' % file_name)

    # 云盘资源统一直链逻辑（115/天翼等全部适用）：换链声明播放端完整 UA
    # （本地代理从真实请求捕获，与 Kodi 原生 curl / FastVFS 默认发送的串
    # 一致），自洽探测（与播放同头的 Range 0-0，仅 206）通过则直接交给播放
    # 器直连 CDN，零代理流量；探测失败或有效期不足（直连后链接中途过期无
    # 自愈，代理才有失效重取）回退本地代理（NAS 中转 / 云链头注入）。
    # 尚无 UA 采样时（安装后首次播放，代理还没见过任何真实请求）先走代理：
    # 首播必然可用，同时由代理完成采样，之后自动切直链。
    # ISO/BDMV 额外受"扩展名必须在 URL path 尾"约束：直链 path 自带 .iso 才能
    # 裸直连；否则优先 VFS 插件（配置了 VFS Scheme 时，描述符自带直链与请求头），
    # 再回退代理（/stream/<guid>/<名>.iso 的装饰段同样满足该规则）。
    # 首播也尝试直链（能直链就直链）：旧策略要求条目曾被代理"换过链"
    # （cloud_known）才尝试直链，导致每个影片第一次打开都是代理。现在
    # 播放端 UA 早已采样固定（client_ua.json），只要 UA 在就尝试直链；
    # NAS 本地文件无直链会自动落回代理，代价只是首播多一次轻量 /stream。
    # 唯一保留的例外：UA 从未采样过（全新安装的第一次播放）仍走代理，
    # 由代理完成采样后下次起全直链。
    total = _to_int(item.get('duration'))
    resume_ts = _to_int(play_info.get('ts'))
    url = None
    # 直链有效期门控（分钟，可调）：115 等网盘直链 TTL 通常仅 35-50 分钟。
    # 默认 30 分钟：低于此值视为"撑不完起播段"，交代理处理。
    gate_min = _to_int(util.get_setting('directttl', '30'))
    min_valid_secs = gate_min * 60

    # 长片防中途断流保护：直连 CDN 模式下 Kodi 无法中途自动换链，若剩余片长
    # 明显超出直链寿命，播到后半段一旦 seek 即被网盘 403 闪退。
    # 片长已知时，若直链无法撑完整场，主动交由本地代理承接（代理在 403 时能自愈重签）。
    remaining_play = (total - resume_ts) if total > 0 else 0
    direct_req_secs = min_valid_secs
    if remaining_play > min_valid_secs and util.get_setting('long_video_proxy_guard', 'true') == 'true':
        direct_req_secs = remaining_play + 300  # 需能撑完剩余片长 + 5分钟缓冲

    if util.get_client_ua():
        playable = None
        try:
            playable = client.resolve_playable_url(
                media_guid, min_valid_secs=direct_req_secs)
            if not playable and direct_req_secs > min_valid_secs:
                util.log('长片剩余时长 %d 分钟超出直链单次有效期，主动走本地代理保障中途换链自愈'
                         % (remaining_play / 60), xbmc.LOGINFO)
        except Exception as e:
            util.log('直链解析失败，回退代理: %s' % e, xbmc.LOGWARNING)
        if playable and file_name.lower().endswith('.iso') \
                and not util.url_path_has_ext(playable['base'], 'iso'):
            vfs_url = _build_vfs_url(playable, file_name)
            if vfs_url:
                util.debug('ISO 直链经 VFS 插件取流: %s' % vfs_url[:120])
                playable = {'url': vfs_url, 'base': ''}
            else:
                util.debug('直链 path 无 .iso 后缀且未配置 VFS，ISO 回退代理')
                playable = None
        if playable:
            url = playable['url']
            util.debug('使用网盘直链（绕过代理）: %s' % (playable['base'] or url)[:120])
    elif cloud_known:
        util.debug('尚无播放端 UA 采样，本次走本地代理（代理将捕获完整 UA，下次起直链）')
    if not url:
        # 光盘镜像嗅探（仅本地代理路径；直链/VFS 给的是 CDN 真实文件字节，
        # 无需嗅探）：fnOS mediasrv 对本地 iso/BDMV 的 media/range 返回转换
        # 后的正片 TS 流而非镜像字节（详见 sniff.py 头注），URL 以 .iso 结尾
        # 会让 Kodi 按镜像挂载必失败（真机 "Error opening image file"）。
        # 转换流把 URL 装饰名换成 .ts 起播（Kodi 直接 demux 正片，无菜单）；
        # 真镜像/云盘 guid 维持 .iso 原路径。结果按 media_guid 落盘缓存。
        url_name = file_name
        if sniff.is_disc_image_name(file_name):
            kind = sniff.probe(client, media_guid, log_tag='%s ' % file_name[:48])
            if kind == sniff.KIND_TS:
                url_name = os.path.splitext(file_name)[0] + '.ts'
                util.log('NAS 返回转换 TS 流，按正片流起播（无菜单）: %s' % file_name)
        url = proxy.stream_url(media_guid, url_name or None)
        if not url:
            util.notify('本地代理启动失败，无法播放', error=True)
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem(offscreen=True))
            return

    li = xbmcgui.ListItem(label=label, path=url)

    # 元数据
    infos = {
        'title': label,
        'plot': item.get('overview', ''),
        'mediatype': 'episode' if play_info.get('type') == 'Episode' else 'movie',
    }
    # 播放中信息页可查到真实文件名与所选版本（多版本影片尤其需要）：
    # 写进剧情首行——播放 OSD 的标题取 title 标签（影片名），文件名
    # 只有这里能可靠展示
    if file_name:
        head = '正在播放：%s' % file_name
        if streams and len(streams) > 1:
            head += '（版本 %d/%d）' % (source_index + 1, len(streams))
        infos['plot'] = head + (('\n' + infos['plot']) if infos['plot'] else '')
    if item.get('tv_title'):
        infos['tvshowtitle'] = item['tv_title']
    if play_info.get('type') == 'Episode':
        if _to_int(item.get('season_number')):
            infos['season'] = _to_int(item['season_number'])
        if _to_int(item.get('episode_number')):
            infos['episode'] = _to_int(item['episode_number'])
    try:
        if float(item.get('vote_average') or 0) > 0:
            infos['rating'] = float(item['vote_average'])
    except (TypeError, ValueError):
        pass
    util.apply_video_info(li, infos)

    # 详情/演员表：仅读落盘缓存（即时 JSON，零 API）；未命中留给 setResolvedUrl
    # 之后的后台线程预热（为下次播放满配）。冷项首播只少 fanart 外的详情标签，
    # 海报/背景图仍来自 play_info 即时设置。
    detail = {}
    persons = []
    try:
        from resources.lib import meta
        cached = meta.get_cached_detail(guid)
        if cached:
            detail, persons = cached.get('detail') or {}, cached.get('persons') or []
    except Exception as e:
        util.log('读取详情缓存失败(不影响播放): %s' % e, xbmc.LOGWARNING)

    try:
        art = {}
        poster = client.image_url(item.get('posters') or item.get('poster') or detail.get('posters'), width=400)
        if poster:
            art.update({'thumb': proxy.image_url(poster), 'poster': proxy.image_url(poster)})
        backdrop = client.image_url(detail.get('backdrops') or item.get('backdrops') or item.get('still_path'), width=1280)
        if backdrop:
            art['fanart'] = proxy.image_url(backdrop)
        logo = client.image_url(detail.get('logos') or item.get('logos'), width=800)
        if logo:
            art['clearlogo'] = proxy.image_url(logo)
        if art:
            li.setArt(art)
    except Exception as e:
        util.log('设置海报失败: %s' % e, xbmc.LOGWARNING)

    # 流派 / 导演 / 年份
    try:
        if detail:
            from resources.lib import meta
            extra = {}
            genres = meta.genre_names(client, detail.get('genres'))
            if genres:
                extra['genre'] = genres
            directors = meta.crew_names(persons, 'Director')
            if directors:
                extra['director'] = directors[:3]
            date = detail.get('release_date') or detail.get('air_date') or ''
            if date[:4].isdigit():
                extra['year'] = int(date[:4])
            if extra:
                util.apply_video_info(li, extra)
    except Exception as e:
        util.log('设置详情标签失败(不影响播放): %s' % e, xbmc.LOGWARNING)

    # 断点续播：NAS 上有进度就默认接着看。
    # 不能用 argv[3] 的 "resume:true|false" 做决策：插件墙/小组件用
    # PlayMedia(path) 起播时，条目没有 Kodi 书签，argv[3] 恒为 resume:false
    # ——这与用户主动选「从头开始」无法区分（0.7.30 据此改过，结果继续观看
    # 全部从 0 起播）。因此只认显式 resume=false（列表右键「从头播放」）。
    resume_ts = _to_int(play_info.get('ts'))
    if total and resume_ts > total * 0.95:
        resume_ts = 0             # 接近片尾视为看完，不 seek 到结尾
    _choice = (params.get('resume') or '').strip().lower()
    seek_to = 0 if _choice in ('false', '0', 'no') else resume_ts
    if seek_to:
        # 交给 Kodi 原生定位：解析结果带 resume point 时 CPluginDirectory 会
        # 强制 STARTOFFSET_RESUME。ISO/BDMV Kodi 不应用该位置，仍由 service
        # 的 onAVStarted 按 pending.seek_to 兜底，near-check 避免重复 seek。
        util.set_resume_point(li, seek_to, total)
    util.log('续播决策: seek_to=%ds resume_ts=%ds resume_param=%s' % (
        seek_to, resume_ts, _choice or '-'))

    # 外挂字幕（仅 stream/list 已就绪时）
    try:
        if subs_event is not None:
            def _ext_count(data):
                return len([s for s in ((data or {}).get('subtitle_streams') or [])
                            if _to_int(s.get('is_external')) == 1])
            if subs_event.wait(2.5) and subs_box.get('data'):
                old_n = _ext_count(stream_list)
                stream_list = subs_box['data']
                new_n = _ext_count(stream_list)
                if new_n != old_n:
                    util.log('外挂字幕清单已更新: %d → %d 条（%.2fs）' % (
                        old_n, new_n, subs_box.get('cost') or 0))
                else:
                    util.debug('外挂字幕清单无变化（%d 条，%.2fs）' % (
                        new_n, subs_box.get('cost') or 0))
            else:
                util.log('字幕清单刷新超时/失败，沿用缓存（%d 条外挂）'
                         % _ext_count(stream_list), xbmc.LOGWARNING)
        if stream_list:
            subtitle_paths = _download_subtitles(
                client, stream_list, media_guid, force=refresh_subs)
            if subtitle_paths:
                li.setSubtitles(subtitle_paths)
    except Exception as e:
        util.log('字幕处理失败: %s' % e, xbmc.LOGWARNING)

    # 写入 pending.json，由 service.py 在播放结束时回传进度
    play_link = urlparse(client.get_video_url(media_guid)).hostname or client.base
    util.save_json(PENDING_FILE, {
        'base': client.base,
        'verify': bool(client.verify),
        'token': client.token,
        'item_guid': play_info.get('guid') or guid,
        'media_guid': media_guid,
        'video_guid': play_info.get('video_guid', ''),
        'audio_guid': play_info.get('audio_guid', ''),
        'subtitle_guid': play_info.get('subtitle_guid', ''),
        'play_link': play_link,
        'ts0': seek_to,          # 起播位置基线（从头开始=0），进度回传的兜底
        'ts': seek_to,           # 最后已知的播放位置（service 每次回传后更新，
                                 # 崩溃/强退后补报取它，取不到才退回 ts0）
        'seek_to': seek_to,      # service.onAVStarted 的自动 seek 目标（0=不 seek）
        'duration': total,
        'stream_url': url,
    })

    via = '直链' if url and '127.0.0.1' not in url else '代理'
    util.log('开始播放: %s (via=%s, file=%s, media=%s, resume=%ds, streams=%d)' % (
        label, via, file_name or '-', media_guid[:16], resume_ts, len(streams)))
    xbmcplugin.setResolvedUrl(handle, True, li)

    # 后台装配 OSD：演员表（最重，最多 40 次 image_url+setCast）+ 冷项详情预热。
    # 已 setResolvedUrl，不阻塞播放器；setCast best-effort 设到 ListItem，
    # 信息面板打开时可见则用，不可见则下次播放 detail 命中即满配。
    import threading

    def _enrich_osd():
        try:
            from resources.lib import meta
            if persons:
                # 暖项：用缓存 persons 装 cast（快，无 API）
                cast = meta.build_cast(client, persons)
                if cast:
                    util.set_video_cast(li, cast)
            elif not detail:
                # 冷项：交 service 进程预热，不在 play invoker 内发 API（会拖住 invoker）
                meta.queue_enrich([guid])
        except Exception as e:
            util.log('后台装配演员表失败(不影响播放): %s' % e, xbmc.LOGDEBUG)

    threading.Thread(target=_enrich_osd, daemon=True).start()


def _version_labels(streams):
    """构造版本选择列表的显示文本"""
    labels = []
    for i, s in enumerate(streams):
        parts = []
        if s.get('title'):
            parts.append(s['title'])
        resolution = s.get('resolution_type') or ''
        if not resolution and s.get('width') and s.get('height'):
            resolution = '%sx%s' % (s['width'], s['height'])
        if resolution:
            parts.append(str(resolution))
        if s.get('codec_name'):
            parts.append(str(s['codec_name']).upper())
        if s.get('is_bluray'):
            parts.append('蓝光')
        labels.append('%d. %s' % (i + 1, ' | '.join(parts) if parts else '版本 %d' % (i + 1)))
    return labels


def select_source(params):
    """弹出版本选择对话框后播放（蓝光原盘多版本入口）"""
    guid = params.get('guid', '')
    client = _prepare()

    stream_list = _get_cached_stream_list(guid)
    if not stream_list:
        util.notify('正在获取版本信息…（冷文件首次可能需要数十秒）')
        try:
            stream_list = client.stream_list(guid)
            if stream_list:
                _store_stream_list(guid, stream_list)
        except ApiError as e:
            util.notify('获取流列表失败：%s' % e.message, error=True)
            return

    streams = stream_list.get('video_streams') or []
    if len(streams) <= 1:
        util.notify('该项目只有一个版本，直接播放')
        url = plugin_url({'action': 'play', 'guid': guid})
        xbmc.executebuiltin('PlayMedia(%s)' % url)
        return

    labels = _version_labels(streams)
    choice = xbmcgui.Dialog().select('选择版本', labels)
    if choice < 0:
        return
    url = plugin_url({'action': 'play', 'guid': guid, 'source': str(choice)})
    xbmc.executebuiltin('PlayMedia(%s)' % url)


def mark_watched(params):
    guid = params.get('guid', '')
    if not guid:
        return
    client = _prepare()
    try:
        client.set_watched(guid)
        client.invalidate_item_list_cache()   # 文件夹视图的原始列表缓存立即失效
        from resources.lib import desc
        desc.patch_watched(guid)              # 整库描述符原地修正，刷新即见已看
        util.notify('已标记为观看')
    except ApiError as e:
        util.notify('标记失败：%s' % e.message, error=True)
    xbmc.executebuiltin('Container.Refresh')


def relogin():
    """强制重新登录并刷新容器"""
    util.set_setting('token', '')
    global_client = util.ensure_client()
    global_client.token = ''
    global_client._token_checked = False
    try:
        global_client.ensure_token()
        util.notify('登录成功')
    except ApiError as e:
        util.notify('登录失败：%s' % e.message, error=True)
    xbmc.executebuiltin('Container.Refresh')
