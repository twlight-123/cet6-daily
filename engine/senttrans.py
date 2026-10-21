# -*- coding: utf-8 -*-
"""划句翻译：在正文里拖选一段英文，给一句读得顺的中文。

两条路，优先大模型（贴合语境、术语准），没配或失败就落到免密钥的
有道 / MyMemory（见 engine.translate）。同一段原文只译一次，结果写进
data/sent_cache.json，重复选同一句零消耗、毫秒级返回。

和「整篇翻译」的区别：整篇是逐段译好存进文章 JSON；这里是用户随手
划的一句话，讲的是别把半句话硬翻成断句，所以给模型的要求更松一点——
按语义补全省略成分，但不许添意思。
"""
import hashlib
import json
import os
import re
import threading
import time

from . import llm, store
from . import translate as free

CACHE_PATH = os.path.join(store.DATA, "sent_cache.json")
MAX_CACHE = 3000
MIN_CHARS = 4          # 少于这个数不值得跑一趟
MAX_CHARS = 700        # 一次最多译这么多字符（约 120 词），够一段了

_lock = threading.Lock()
_cache = None

SENT_SYSTEM = (
    "你是英语阅读时的即时翻译工具。只输出中文译文，不要解释、不要加引号、"
    "不要复述原文、不要写「这句话的意思是」。专有名词用通行译名，人名地名保留惯用写法。"
)

SENT_PROMPT = """把下面这段英文译成自然的中文。原文是从一篇文章里划出来的，可能不是完整句子：

1. 译文要读起来像中文里本来就这么写，不要逐词直译；
2. 原文若有省略或指代，按上下文补顺，但不要增加原文没有的意思；
3. 只给译文，一段话，不要分行，不要编号。

{text}"""


def _load():
    global _cache
    if _cache is None:
        _cache = store.load_json(CACHE_PATH, None) or {}
    return _cache


def _trim(c):
    if len(c) <= MAX_CACHE:
        return
    old = sorted(c.items(), key=lambda kv: kv[1].get("ts", 0))
    for k, _ in old[: len(c) - MAX_CACHE]:
        c.pop(k, None)


def _key(t):
    return hashlib.md5(t.encode("utf-8")).hexdigest()[:20]


def normalize(text):
    """折叠空白、去掉软连字符——从网页里选出来的文本常带换行和零宽字符。"""
    t = (text or "").replace("\u00ad", "").replace("\u200b", "")
    return re.sub(r"\s+", " ", t).strip()


def clean(text):
    t = normalize(text)
    if len(t) < MIN_CHARS:
        raise ValueError("选中的文字太短")
    return t[:MAX_CHARS]


def cn_len(s):
    return len(re.findall(r"[\u4e00-\u9fff]", s or ""))


def _by_llm(settings, text):
    """返回中文译文，失败抛异常。"""
    out = llm.chat(settings, [
        {"role": "system", "content": SENT_SYSTEM},
        {"role": "user", "content": SENT_PROMPT.format(text=text)},
    ], temperature=0.25, max_tokens=min(1400, 260 + len(text)), timeout=40)
    zh = (out or "").strip()
    # 模型偶尔会把原文照抄一遍，或者只回一句「这是……的翻译」
    zh = re.sub(r"^(译文|中文|翻译)[:：]\s*", "", zh).strip()
    zh = zh.strip("「」“”\"' ")
    if cn_len(zh) < 2:
        raise RuntimeError("译文里没有中文")
    return zh


def translate(text, use_llm=True):
    """返回 {en, zh, source, cached}；source ∈ llm / free。"""
    t = clean(text)
    k = _key(t)
    c = _load()
    hit = c.get(k)
    if hit and hit.get("zh"):
        out = dict(hit)
        out["cached"] = True
        return out

    settings = store.settings()
    zh, src = "", "free"
    if use_llm and llm.is_configured(settings):
        try:
            zh = _by_llm(settings, t)
            src = "llm"
        except Exception:
            zh = ""
    if not zh:
        zh = free.translate(t)
        src = "free"
    zh = (zh or "").strip()
    if not zh:
        raise RuntimeError("翻译失败，换成大模型或稍后再试")

    rec = {"en": t, "zh": zh, "source": src, "ts": time.time()}
    with _lock:
        c[k] = rec
        _trim(c)
        store.save_json(CACHE_PATH, c)
    out = dict(rec)
    out["cached"] = False
    return out


def stats():
    c = _load()
    return {"count": len(c)}
