"""课外补充阅读：中英对照，不出题。

素材两类：
  1. 内置文库（data/reading/*.json）——历史故事、小说节选、历史文化、时事热点，
     人工选编并翻译，不联网也能用，按日期轮换；
  2. 当日生成（配置了大模型时）——把当天抓到的新闻改写成一段并翻译，
     存成同格式，优先级高于文库。

一篇的结构：
    {"id","date","kicker","title_en","title_zh","source","minutes",
     "paragraphs":[{"en","zh"}], "vocab":[{"word","gloss"}]}
"""
import datetime
import glob
import json
import os
import re

from . import store

_DIR = os.path.join(store.DATA, "reading")

KIND_LABEL = {"news": "时事热点", "history": "历史故事", "fiction": "小说节选",
              "culture": "历史文化", "science": "科技前沿"}


def ensure():
    os.makedirs(_DIR, exist_ok=True)


def _path(rid):
    return os.path.join(_DIR, "%s.json" % rid)


def _safe_id(text):
    return re.sub(r"[^a-z0-9\-]+", "-", (text or "reading").lower()).strip("-")[:48] or "reading"


def load_all():
    ensure()
    out = []
    for p in glob.glob(os.path.join(_DIR, "*.json")):
        d = store.load_json(p, None)
        if d and d.get("paragraphs"):
            d["_path"] = p
            out.append(d)
    out.sort(key=lambda d: (d.get("date") or "", d.get("id") or ""))
    return out


def get(rid):
    for d in load_all():
        if d.get("id") == rid:
            d.pop("_path", None)
            return d
    return None


def save(item):
    ensure()
    rid = item.get("id") or _safe_id(item.get("title_en"))
    item["id"] = rid
    store.save_json(_path(rid), item)
    return rid


def pick(date_str=None, kind=None):
    """挑当天要读的那篇：优先当日生成，其次按日期在文库内轮换。"""
    items = load_all()
    if kind:
        items = [x for x in items if x.get("kind") == kind] or items
    if not items:
        return None
    day = date_str or datetime.date.today().isoformat()

    for it in items:                       # 当天专门生成或指定过的
        if it.get("date") == day and it.get("source") == "daily":
            return _clean(it)

    pool = [x for x in items if x.get("source") != "daily"]
    if not pool:
        pool = items
    try:
        ordinal = datetime.date.fromisoformat(day).toordinal()
    except Exception:
        ordinal = 0
    return _clean(pool[ordinal % len(pool)])


def brief(item):
    """首页卡片只要开头一小段，不把全文塞进 bootstrap。"""
    if not item:
        return None
    paras = item.get("paragraphs") or []
    first = paras[0] if paras else {"en": "", "zh": ""}
    return {
        "id": item.get("id"),
        "date": item.get("date") or "",
        "words": sum(len((p.get("en") or "").split()) for p in paras),
        "kicker": KIND_LABEL.get(item.get("kind"), item.get("kicker") or "课外阅读"),
        "title_en": item.get("title_en"),
        "title_zh": item.get("title_zh"),
        "source": item.get("source_label") or item.get("source") or "",
        "fallback": item.get("source") != "daily",
        "translator": item.get("translator") or ("llm" if item.get("source") == "daily" else "manual"),
        "minutes": item.get("minutes") or max(2, round(
            sum(len(p.get("en", "").split()) for p in paras) / 120)),
        "lead_en": first.get("en", ""),
        "lead_zh": first.get("zh", ""),
        "paragraphs": len(paras),
    }


def _clean(item):
    return {k: v for k, v in item.items() if k != "_path"}


def with_hard(item):
    """给每段附上「值得查」的词，前端据此给六级词加下划线。

    正文里每个词都能点，但满屏虚线没法读；只标六级及以上，
    学生一眼就知道哪些词该留意。
    """
    if not item:
        return item
    from . import vocab
    out = dict(item)
    paras = []
    for p in item.get("paragraphs") or []:
        hard = set()
        for m in re.finditer(r"[A-Za-z][A-Za-z'\-]*", p.get("en") or ""):
            w = m.group(0)
            if len(w) >= 6 and w.lower() not in hard:
                tier = vocab.lookup(w)[0]
                if tier in ("cet6", "beyond"):
                    hard.add(w.lower())
        paras.append({"en": p.get("en", ""), "zh": p.get("zh", ""), "hard": sorted(hard)})
    out["paragraphs"] = paras
    return out
