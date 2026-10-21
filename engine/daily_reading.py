"""每日课外阅读：抓当天的一篇文章，切段、翻译、存成中英对照。

和套题一样跟着日期走——今天做完明天就换一篇新的，不是几篇轮着放。
翻译走 engine/translate（免密钥），译不出来就退回内置文库，绝不空着。
"""
import datetime
import re
import threading

from . import llm, reading, sources, store, translate
from .textutil import split_sentences

_wanted = set()
_lock = threading.Lock()


def _words(s):
    return len((s or "").split())


def _chunks(paras, min_words=200, max_words=560, max_chunks=10):
    """尽量保留整段、整篇：200–560 词能装下就用全文，超了才按整段截。

    以前把长段按句切碎、上限 300 词，读起来像摘要，衔接全丢；
    现在优先完整——段落是原文的论证单元，切口留在段与段之间才不伤意思。
    """
    units = [(p or "").strip() for p in paras]
    units = [p for p in units if _words(p) >= 15]
    if not units:
        return []

    total = sum(_words(p) for p in units)
    if total <= max_words:
        return units[:max_chunks] if len(units) > max_chunks else units

    out, n = [], 0
    for p in units:
        w = _words(p)
        if n and n + w > max_words:
            break
        out.append(p)
        n += w
        if len(out) >= max_chunks:
            break

    # 前面几段太短时补到 200 词，避免只剩一段
    rest = units[len(out):]
    while n < min_words and rest:
        p = rest.pop(0)
        out.append(p)
        n += _words(p)
    return out


def build_from_article(art, date_str=None):
    """把一篇文章做成当日读物；失败返回 None。"""
    date_str = date_str or datetime.date.today().isoformat()
    paras = _chunks(art.get("paragraphs") or [art.get("text", "")])
    paras = [p.strip() for p in paras if _words(p) >= 30]
    total_words = sum(_words(p) for p in paras)
    if total_words < 190 or (len(paras) < 2 and total_words < 300):
        return None

    title_en = art.get("title") or "Today's Reading"
    if len(title_en) > 88:                      # 新闻标题常常一整句，截到第一个分句
        cut = re.split(r"(?<=[.?!])\s|\s—\s|:\s", title_en)[0]
        title_en = (cut if len(cut) > 18 else title_en[:84]).strip()
    try:
        title_zh = translate.translate(title_en)
    except Exception:
        title_zh = ""

    # 配了大模型就用它译，中文质量高一档；没配或失败再走机翻
    zh_list, translator = [], "mt"
    st = store.settings()
    if llm.is_configured(st):
        try:
            zh_list = llm.translate_blocks(st, title_en, paras)
            translator = "llm"
        except Exception:
            zh_list = []

    if not zh_list:
        zh_list = []
        for p in paras:
            try:
                zh_list.append(translate.translate(p))
            except Exception:
                zh_list.append("")

    pairs = [{"en": p, "zh": z} for p, z in zip(paras, zh_list)]
    if not any(x["zh"] for x in pairs):
        return None

    topic = art.get("topic") or "news"
    kind = {"research": "science", "news": "news", "history": "history"}.get(topic, "news")

    vocab_out = []      # 自动生成不附词表：词典义脱离语境容易误导

    item = {
        "id": "daily-%s" % date_str,
        "kind": kind,
        "source": "daily",
        "date": date_str,
        "source_label": "每日一文 · %s" % (art.get("source") or ""),
        "title_en": title_en,
        "title_zh": title_zh,
        "minutes": max(3, round(sum(_words(p) for p in paras) / 140)),
        "paragraphs": pairs,
        "vocab": vocab_out,
        "translator": translator,
        "url": art.get("url", ""),
    }
    reading.save(item)
    return item


def _from_network(date_str):
    """没有现成文章时，自己抓一篇。"""
    cands, _errs = sources.gather(limit_per_feed=4)
    if not cands:
        return None
    import random
    random.shuffle(cands)
    # 有些源（如 Ars Technica）会对抓取返回 405，多试几篇，别被一个源卡死
    for i, c in enumerate(cands):
        if i >= 14:
            break
        art = sources.build_article(c)
        if not art:
            continue
        item = build_from_article(art, date_str)
        if item:
            return item
    return None


def pending():
    """是否正在后台生成（前端据此轮询刷新）。"""
    return bool(_wanted)


def ensure(date_str=None, background=True):
    """确保当天有一篇「每日一文」。已有就返回，没有就生成。"""
    date_str = date_str or datetime.date.today().isoformat()
    for it in reading.load_all():
        if it.get("date") == date_str and it.get("source") == "daily":
            return it

    with _lock:
        if date_str in _wanted:
            return None
        _wanted.add(date_str)

    def work():
        try:
            return _from_network(date_str)
        except Exception:
            return None
        finally:
            with _lock:
                _wanted.discard(date_str)

    if background:
        threading.Thread(target=work, daemon=True, name="daily-reading").start()
        return None
    return work()
