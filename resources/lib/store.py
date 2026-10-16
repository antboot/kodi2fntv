# -*- coding: utf-8 -*-
"""轻量持久化存储层（基于 Python 内置 sqlite3）

替代原先散落全盘的数千个小 JSON 缓存文件（detail_*.json、watch_recent 等）：
- 启用 WAL 模式，天然支持多进程（Kodi invoker 与后台 service）并发安全读写
- 单文件聚合存储，消除电视盒子 Flash/eMMC 碎文件 4K 随机 I/O 磨损与遍历卡顿
- 进程内带高频热点内存缓存，查询速度达微秒级
- 向后兼容：读取未命中时自动从旧版 JSON 文件读入并迁移入库，无缝升级
"""
import json
import os
import sqlite3
import threading
import time

import xbmc

from resources.lib import util

_DB_FILE = 'fnmedia.db'
_init_lock = threading.Lock()
_initialized = False

# 进程内热点缓存
_detail_memo = {}       # guid -> (expire_ts, data)
_kv_memo = {}           # key -> (expire_ts, value)
_MEMO_MAX = 500


def _db_path():
    return os.path.join(util.addon_data_dir(), _DB_FILE)


def _connect():
    """获取数据库连接并开启 WAL 模式与忙等待超时"""
    global _initialized
    db_file = _db_path()
    try:
        os.makedirs(os.path.dirname(db_file), exist_ok=True)
    except OSError:
        pass

    conn = sqlite3.connect(db_file, timeout=10.0)
    conn.row_factory = sqlite3.Row
    if not _initialized:
        with _init_lock:
            if not _initialized:
                try:
                    conn.execute('PRAGMA journal_mode=WAL')
                    conn.execute('PRAGMA synchronous=NORMAL')
                    conn.execute('''
                        CREATE TABLE IF NOT EXISTS details (
                            guid TEXT PRIMARY KEY,
                            ts REAL,
                            detail TEXT,
                            persons TEXT
                        )
                    ''')
                    conn.execute('''
                        CREATE TABLE IF NOT EXISTS kv (
                            key TEXT PRIMARY KEY,
                            val TEXT,
                            ts REAL,
                            expire_ts REAL
                        )
                    ''')
                    conn.commit()
                    _initialized = True
                except Exception as e:
                    util.log('初始化 sqlite 失败: %s' % e, xbmc.LOGWARNING)
    return conn


# ----------------------------------------------------------------------
# 详情缓存 (Details Cache: 替换 detail_*.json)
# ----------------------------------------------------------------------

DETAIL_TTL = 7 * 86400  # 7 天


def get_detail(guid):
    """读取单条条目详情与演职员，优先内存缓存，其次 SQLite，兜底自动迁移旧 JSON"""
    if not guid:
        return None
    now = time.time()
    hit = _detail_memo.get(guid)
    if hit and hit[0] > now:
        return hit[1]

    # 1. 查 SQLite
    entry = None
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute('SELECT ts, detail, persons FROM details WHERE guid = ?', (guid,))
            row = cur.fetchone()
            if row:
                if now - row['ts'] < DETAIL_TTL:
                    entry = {
                        'ts': row['ts'],
                        'detail': json.loads(row['detail']),
                        'persons': json.loads(row['persons']),
                    }
    except Exception as e:
        util.log('读取详情 DB 异常 %s: %s' % (guid, e), xbmc.LOGDEBUG)

    # 2. 兜底检查旧版单文件 JSON，若有则自动迁移入库
    if not entry:
        old_path = os.path.join(util.addon_data_dir(), 'detail', guid + '.json')
        if os.path.isfile(old_path):
            try:
                old_data = util.load_json(os.path.join('detail', guid + '.json'))
                if isinstance(old_data, dict) and now - old_data.get('ts', 0) < DETAIL_TTL:
                    entry = old_data
                    store_detail(guid, entry.get('detail'), entry.get('persons'), ts=entry.get('ts'))
                    # 迁移完成后清理旧文件
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
            except Exception:
                pass

    if entry:
        if len(_detail_memo) >= _MEMO_MAX:
            _detail_memo.clear()
        _detail_memo[guid] = (now + 60, entry)  # 内存记忆 60 秒
    return entry


def store_detail(guid, detail, persons, ts=None):
    """保存详情与演职员到 SQLite，并更新内存缓存"""
    if not guid:
        return
    now = ts or time.time()
    d_str = json.dumps(detail or {}, ensure_ascii=False)
    p_str = json.dumps(persons or [], ensure_ascii=False)

    try:
        with _connect() as conn:
            conn.execute('''
                INSERT INTO details (guid, ts, detail, persons)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guid) DO UPDATE SET
                    ts=excluded.ts,
                    detail=excluded.detail,
                    persons=excluded.persons
            ''', (guid, now, d_str, p_str))
            conn.commit()
    except Exception as e:
        util.log('写入详情 DB 失败 %s: %s' % (guid, e), xbmc.LOGWARNING)

    # 更新进程内缓存
    if len(_detail_memo) >= _MEMO_MAX:
        _detail_memo.clear()
    _detail_memo[guid] = (time.time() + 60, {'ts': now, 'detail': detail, 'persons': persons})


def all_details():
    """遍历所有详情 (guid, detail_dict)，用于本地流派索引构建等批量任务，零小文件 I/O"""
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute('SELECT guid, detail FROM details')
            for row in cur.fetchall():
                try:
                    yield row['guid'], json.loads(row['detail'])
                except Exception:
                    continue
    except Exception as e:
        util.log('all_details 失败: %s' % e, xbmc.LOGDEBUG)



# ----------------------------------------------------------------------
# 键值缓存 (KV Store: 替换 watch_recent, sniff_cache, art_miss 等)
# ----------------------------------------------------------------------

def get_kv(key, default=None):
    """读取通用键值，自动解析 JSON 并做 TTL 过期判断"""
    if not key:
        return default
    now = time.time()
    hit = _kv_memo.get(key)
    if hit and hit[0] > now:
        return hit[1]

    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute('SELECT val, expire_ts FROM kv WHERE key = ?', (key,))
            row = cur.fetchone()
            if row:
                expire_ts = row['expire_ts']
                if expire_ts == 0 or expire_ts > now:
                    val = json.loads(row['val'])
                    _kv_memo[key] = (now + 30, val)
                    return val
                else:
                    # 已过期，顺手清理
                    conn.execute('DELETE FROM kv WHERE key = ?', (key,))
                    conn.commit()
    except Exception as e:
        util.log('get_kv 失败 %s: %s' % (key, e), xbmc.LOGDEBUG)

    return default


def set_kv(key, val, ttl=None):
    """存储通用键值，ttl 为过期秒数（None 表示永不过期）"""
    if not key:
        return
    now = time.time()
    expire_ts = (now + ttl) if ttl else 0
    val_str = json.dumps(val, ensure_ascii=False)

    try:
        with _connect() as conn:
            conn.execute('''
                INSERT INTO kv (key, val, ts, expire_ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    val=excluded.val,
                    ts=excluded.ts,
                    expire_ts=excluded.expire_ts
            ''', (key, val_str, now, expire_ts))
            conn.commit()
    except Exception as e:
        util.log('set_kv 失败 %s: %s' % (key, e), xbmc.LOGWARNING)

    _kv_memo[key] = (now + 30, val)


def del_kv(key):
    """删除通用键值"""
    if not key:
        return
    _kv_memo.pop(key, None)
    try:
        with _connect() as conn:
            conn.execute('DELETE FROM kv WHERE key = ?', (key,))
            conn.commit()
    except Exception as e:
        util.log('del_kv 失败 %s: %s' % (key, e), xbmc.LOGDEBUG)
