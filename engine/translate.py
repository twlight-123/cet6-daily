"""英文→中文翻译（免密钥，联网）。

引擎按质量排队，前一个失败才用后一个：
  1. 有道 aidemo —— 中文最自然，单次可处理七八百字符；
  2. MyMemory —— 兜底，偏直译。

译过的片段写进 data/translate_cache.json，同一句永不重复请求。
这一层不需要大模型；配置了模型的用户，另走 engine.llm.generate_reading 拿润色版。
"""
import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.request

from . import store

CACHE_PATH = os.path.join(store.DATA, "translate_cache.json")
MAX_CACHE = 4000
PIECE_LIMIT = 800
_lock = threading.Lock()
_cache = None
_last_call = [0.0]
MIN_GAP = 0.4

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 机器翻译仍会译错的固定说法
_GLOSSARY = [
    (r"霜冻交易会|霜冻市集|冰冻集市", "冰上集市"),
    (r"角角恐龙", "角龙"),
    (r"褶边", "颈盾"),
    (r"厄尔尼诺\s*Niño", "厄尔尼诺现象"),
    (r"这些电波", "这些波"),
    (r"正常电波", "正常波"),
    (r"戏剧性地", "急剧"),
    (r"令人震惊的", "惊人的"),
]

_bing = {"ig": "", "iid": "translator.5023", "key": 0, "token": "", "ts": 0.0}


def _load_cache():
    global _cache
    if _cache is None:
        _cache = store.load_json(CACHE_PATH, None) or {}
    return _cache


def _save_cache():
    c = _load_cache()
    if len(c) > MAX_CACHE:
        for k in list(c)[: len(c) - MAX_CACHE]:
            c.pop(k, None)
    store.save_json(CACHE_PATH, c)


def _key(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------- 引擎 ----------------
def _bing_token(timeout=15):
    """Bing 翻译页里藏着 IG / IID / key，取一次用两小时。"""
    now = time.time()
    if _bing["ig"] and now - _bing["ts"] < 7200:
        return _bing
    req = urllib.request.Request("https://www.bing.com/translator",
                                 headers={"User-Agent": UA,
                                          "Accept-Language": "zh-CN,zh;q=0.9"})
    with _opener().open(req, timeout=timeout) as r:
        html = r.read().decode("utf-8", "ignore")
    ig = re.search(r'IG:"([0-9A-F]+)"', html)
    iid = re.search(r'data-iid="(translator\.[0-9]+)"', html)
    abuse = re.search(r'params_AbusePreventionHelper\s*=\s*\[(\d+),"([^"]+)"', html)
    if not ig:
        raise RuntimeError("bing token not found")
    _bing.update({"ig": ig.group(1),
                  "iid": iid.group(1) if iid else "translator.5023",
                  "key": int(abuse.group(1)) if abuse else 0,
                  "token": abuse.group(2) if abuse else "",
                  "ts": now})
    return _bing


def _bing_tr(text, timeout=15):
    tok = _bing_token(timeout)
    url = ("https://www.bing.com/ttranslatev3?isVertical=1&&IG=%s&IID=%s.1"
           % (tok["ig"], tok["iid"]))
    body = urllib.parse.urlencode({"fromLang": "en", "to": "zh-Hans",
                                   "text": text,
                                   "key": tok["key"], "token": tok["token"]}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://www.bing.com/translator",
        "Origin": "https://www.bing.com",
    })
    with _opener().open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if isinstance(data, dict) and data.get("statusCode"):
        raise RuntimeError("bing statusCode=%s" % data.get("statusCode"))
    item = data[0] if isinstance(data, list) and data else {}
    out = item.get("translations", [{}])[0].get("text", "")
    if not out:
        raise RuntimeError("bing empty")
    return out


def _youdao(text, timeout=15):
    url = ("https://aidemo.youdao.com/trans?q=" + urllib.parse.quote(text)
           + "&from=en&to=zh-CHS")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with _opener().open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if str(data.get("errorCode", "0")) != "0":
        raise RuntimeError("youdao errorCode=%s" % data.get("errorCode"))
    tr = data.get("translation") or []
    if not tr:
        raise RuntimeError("youdao empty")
    return "".join(tr)


def _mymemory(text, timeout=15):
    url = ("https://api.mymemory.translated.net/get?q="
           + urllib.parse.quote(text) + "&langpair=en|zh-CN")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with _opener().open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if str(data.get("responseStatus")) != "200":
        raise RuntimeError(data.get("responseDetails") or "mymemory failed")
    out = (data.get("responseData") or {}).get("translatedText") or ""
    if not out or "MYMEMORY WARNING" in out.upper():
        raise RuntimeError(out or "empty translation")
    time.sleep(1.5)                      # 这个源限流很紧
    return out


# Bing 的 ttranslatev3 目前返回空响应，先不用；留着实现在这里，通了把顺序调前即可。
ENGINES = [("youdao", _youdao), ("mymemory", _mymemory)]


def _polish(zh):
    """清掉机翻腔：多余空格、重复虚词、标点后空格。"""
    zh = re.sub(r"\s{2,}", " ", zh).strip()
    zh = zh.replace(" ，", "，").replace(" 。", "。").replace(" ：", "：")
    zh = re.sub(r"的的", "的", zh)
    zh = re.sub(r"了了", "了", zh)
    zh = re.sub(r"(?<=[。！？；])\s+", "", zh)
    for pat, rep in _GLOSSARY:
        zh = re.sub(pat, rep, zh)
    return zh


def _split_for_api(text, limit=PIECE_LIMIT):
    """按句子切成不超过 limit 字符的片段，避免接口长度上限。"""
    from .textutil import split_sentences
    sents = split_sentences(text) or [text]
    out, buf = [], ""
    for s in sents:
        while len(s) > limit:
            if buf:
                out.append(buf)
                buf = ""
            out.append(s[:limit])
            s = s[limit:]
        if len(buf) + len(s) + 1 > limit and buf:
            out.append(buf)
            buf = s
        else:
            buf = (buf + " " + s).strip()
    if buf:
        out.append(buf)
    return out or [text]


def _translate_one(text, timeout=15):
    gap = time.time() - _last_call[0]
    if gap < MIN_GAP:
        time.sleep(MIN_GAP - gap)
    _last_call[0] = time.time()

    last_err = None
    for name, fn in ENGINES:
        try:
            out = fn(text, timeout=timeout)
            if out and len(out.strip()) >= max(2, len(text) * 0.05):
                return out
        except Exception as e:
            last_err = e
    raise RuntimeError("翻译失败：%s" % repr(last_err)[:120])


def translate(text, timeout=15):
    """返回中文；三个引擎都失败才抛异常，由调用方降级。"""
    text = (text or "").strip()
    if not text:
        return ""
    cache = _load_cache()
    k = _key(text)
    if k in cache:
        return _polish(cache[k])

    pieces = _split_for_api(text)
    parts = []
    for piece in pieces:
        pk = _key(piece)
        if pk in cache:
            parts.append(cache[pk])
            continue
        parts.append(_translate_one(piece, timeout))

    zh = _polish("".join(parts))
    with _lock:
        cache[k] = zh
        for piece, part in zip(pieces, parts):
            cache[_key(piece)] = part
        _save_cache()
    return zh


def available():
    socket.setdefaulttimeout(12)
    try:
        return bool(translate("The river froze."))
    except Exception:
        return False
