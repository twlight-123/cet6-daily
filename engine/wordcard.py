# -*- coding: utf-8 -*-
"""点词查义：给课外阅读做一个「扇贝式」词卡。

分三层拼一张卡，谁有就用谁的，缺的由别人补：
  1. 本地考纲词表（cet4/cet6.tsv）——离线也有中文释义，零延迟；
  2. dictionaryapi.dev —— 免费、无 key，给音标、英文释义、英文例句；
  3. 大模型（配了才用）—— 一次给全：音标、语境义、词性、例句与译文，质量最高。

同一个词只查一次，结果永久落在 data/dict_cache.json，之后毫秒级返回。
"""
import concurrent.futures
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from . import llm, store, vocab

CACHE_PATH = os.path.join(store.DATA, "dict_cache.json")
MAX_CACHE = 4000

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 词性中文名，卡片上按学生习惯显示
POS_ZH = {
    "n": "名词", "noun": "名词", "v": "动词", "verb": "动词",
    "vt": "及物动词", "vi": "不及物动词", "adj": "形容词", "adjective": "形容词",
    "adv": "副词", "adverb": "副词", "prep": "介词", "preposition": "介词",
    "conj": "连词", "conjunction": "连词", "pron": "代词", "pronoun": "代词",
    "num": "数词", "art": "冠词", "int": "感叹词", "interjection": "感叹词",
}

LEVEL_ZH = {"cet4": "CET-4", "cet6": "CET-6", "common": "基础", "beyond": "超纲"}

_lock = threading.Lock()
_cache = None
_llm_fail = {"n": 0, "until": 0.0}


def _load():
    global _cache
    if _cache is None:
        _cache = store.load_json(CACHE_PATH, None) or {}
    return _cache


def _save():
    c = _load()
    if len(c) > MAX_CACHE:
        for k in list(c)[: len(c) - MAX_CACHE]:
            c.pop(k, None)
    store.save_json(CACHE_PATH, c)


def clean_word(word):
    w = re.sub(r"[^A-Za-z'\-]", "", (word or "")).strip("'-").lower()
    return w if 2 <= len(w) <= 40 else ""


def pos_label(pos):
    p = (pos or "").strip().lower().rstrip(".")
    return POS_ZH.get(p, POS_ZH.get(p.split("/")[0], pos or ""))


# ---------------- 英文词典 ----------------
def _dict_api(word, timeout=6):
    url = "https://api.dictionaryapi.dev/api/v2/entries/en/" + urllib.parse.quote(word)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, list) or not data:
        return None
    head = data[0]

    phonetic = (head.get("phonetic") or "").strip()
    if not phonetic:
        for p in head.get("phonetics") or []:
            if (p.get("text") or "").strip():
                phonetic = p["text"].strip()
                break

    entries = []
    for m in (head.get("meanings") or [])[:3]:
        pos = (m.get("partOfSpeech") or "").strip()
        defs = m.get("definitions") or []
        if not defs:
            continue
        # 优先挑带例句的定义，卡片上例句比释义更值钱
        pick = next((d for d in defs if d.get("example")), defs[0])
        entries.append({
            "pos": pos,
            "def_en": (pick.get("definition") or "").strip()[:220],
            "example": (pick.get("example") or "").strip(),
        })
    if not entries and not phonetic:
        return None
    return {"phonetic": phonetic, "entries": entries}


def _dict_api_retry(word, tries=2):
    last = None
    for _ in range(tries):
        try:
            out = _dict_api(word)
            if out:
                return out
        except Exception as e:
            last = e
    if last:
        raise last
    return None


def _youdao_zh(word, timeout=6):
    """考纲词表没有的词（glacier、crampon…）用有道教一个中文释义。"""
    url = ("https://aidemo.youdao.com/trans?q=" + urllib.parse.quote(word)
           + "&from=en&to=zh-CHS")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if str(data.get("errorCode", "0")) != "0":
        return ""
    tr = data.get("translation") or []
    zh = "".join(tr).strip()
    return zh[:60] if zh and zh.lower() != word.lower() else ""


# ---------------- 大模型 ----------------
CARD_SYSTEM = ("你是《牛津高阶英汉双解词典》的资深编者，为备考六级的中文学生写词条。"
               "释义要准确、克制，按语境取义，不要罗列生僻义项。只输出 JSON。")

CARD_PROMPT = """单词：{word}
所在句子：{ctx}

请给出这个词的词条。

要求：
1. lemma 写成词典原形（如 abandoned → abandon；running → run）；
2. phonetic 只给一个常用 IPA 音标，用 / / 包起来；
3. entries 给 1–2 条，第一条必须是**这个句子里的那个义项**；
4. gloss 是简明中文释义，多个近义用「；」分隔，不超过 24 字，不要写词性前缀；
5. def_en 用一句简单英文解释该义项（不超过 20 词）；
6. example 给一个 8–18 词的英文例句，用上该词；example_zh 是它的自然中文翻译；
7. level 从 CET-4 / CET-6 / 超纲 三者中选一个。

只输出：
{{"lemma":"...","phonetic":"/.../","level":"...","entries":[{{"pos":"vt","gloss":"...","def_en":"...","example":"...","example_zh":"..."}}]}}"""


def _llm_card(settings, word, ctx):
    """返回 dict 或 None。连续失败会短暂熔断，避免每次点词都白等。"""
    if _llm_fail["until"] > time.time():
        return None
    try:
        out = llm.chat(settings, [
            {"role": "system", "content": CARD_SYSTEM},
            {"role": "user", "content": CARD_PROMPT.format(word=word, ctx=(ctx or "")[:320])},
        ], temperature=0.2, max_tokens=700, timeout=20)
        data = llm._extract_json(out or "")
        if not isinstance(data, dict) or not data.get("entries"):
            raise RuntimeError("结构不符")
        entries = []
        for e in data["entries"][:2]:
            if not isinstance(e, dict) or not (e.get("gloss") or "").strip():
                continue
            entries.append({
                "pos": (e.get("pos") or "").strip(),
                "gloss": (e.get("gloss") or "").strip()[:40],
                "def_en": (e.get("def_en") or "").strip()[:220],
                "example": (e.get("example") or "").strip()[:240],
                "example_zh": (e.get("example_zh") or "").strip()[:160],
            })
        if not entries:
            raise RuntimeError("词条为空")
        _llm_fail["n"] = 0
        return {
            "lemma": re.sub(r"[^a-z\-']", "", (data.get("lemma") or word).lower()),
            "phonetic": (data.get("phonetic") or "").strip()[:40],
            "level": (data.get("level") or "").strip()[:10],
            "entries": entries,
        }
    except Exception:
        _llm_fail["n"] += 1
        if _llm_fail["n"] >= 3:
            _llm_fail["until"] = time.time() + 1800
            _llm_fail["n"] = 0
        return None


# ---------------- 组装 ----------------
def _build(word, ctx):
    lemma = vocab.lemma(word) or word
    tier, pos_local, gloss_local = vocab.lookup(word)

    settings = store.settings()
    use_llm = llm.is_configured(settings)

    dict_info, llm_info, zh_hint = None, None, ""
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f_dict = ex.submit(_dict_api_retry, lemma)
        f_llm = ex.submit(_llm_card, settings, word, ctx) if use_llm else None
        f_zh = ex.submit(_youdao_zh, lemma) if not gloss_local else None
        try:
            dict_info = f_dict.result(timeout=10)
        except Exception:
            dict_info = None
        if f_llm is not None:
            try:
                llm_info = f_llm.result(timeout=22)
            except Exception:
                llm_info = None
        if f_zh is not None:
            try:
                zh_hint = f_zh.result(timeout=8) or ""
            except Exception:
                zh_hint = ""

    phonetic = ""
    if llm_info:
        phonetic = llm_info.get("phonetic") or ""
    if not phonetic and dict_info:
        phonetic = dict_info.get("phonetic") or ""

    entries = []
    if llm_info:
        entries = [e for e in llm_info["entries"] if e.get("gloss")]

    if not entries:
        # 没有模型：本地词表给中文，词典给英文，两边拼一条完整的
        base = {"pos": pos_local or "", "gloss": gloss_local or zh_hint or "",
                "def_en": "", "example": "", "example_zh": ""}
        if dict_info and dict_info.get("entries"):
            first = dict_info["entries"][0]
            base["def_en"] = first.get("def_en", "")
            base["example"] = first.get("example", "")
            if not base["pos"]:
                base["pos"] = first.get("pos", "")
        entries = [base] if (base["gloss"] or base["def_en"]) else []
        # 词典的其余义项作为补充（只带英文释义）
        seen = {base.get("def_en", "")}
        for extra in (dict_info or {}).get("entries", [])[1:3]:
            d = (extra.get("def_en") or "").strip()
            if not d or d in seen:
                continue
            seen.add(d)
            entries.append({"pos": extra.get("pos", ""), "gloss": "",
                            "def_en": d, "example": extra.get("example", ""),
                            "example_zh": ""})

    for e in entries:
        e["pos_zh"] = pos_label(e.get("pos"))

    level = (llm_info or {}).get("level") or LEVEL_ZH.get(tier, "")
    if level == "CET-4" and tier == "cet6":
        level = "CET-6"

    return {
        "word": word,
        "lemma": llm_info.get("lemma") if (llm_info and llm_info.get("lemma")) else lemma,
        "phonetic": phonetic,
        "level": level,
        "entries": entries[:3],
        "source": "llm" if llm_info else ("dict" if dict_info else "local"),
    }


def _warm_audio(word):
    """卡片一出来就在后台把发音合成好，点喇叭时不用等。"""
    def run():
        try:
            from . import voice
            voice.get_audio(word, "N", 0.92)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True, name="word-tts").start()


def _candidates(w):
    yield w
    if re.search(r"['’]s$", w):          # NASA's / moon's → 去所有格
        yield w[:-2]
    if w.endswith("ly") and len(w) > 5:
        yield w[:-2]


def lookup(word, ctx=""):
    """返回词卡 dict；查不到抛 LookupError。"""
    w = clean_word(word)
    if not w:
        raise LookupError("无效单词")
    cache = _load()

    for cand in _candidates(w):
        hit = cache.get(cand)
        if hit and hit.get("entries"):
            _warm_audio(hit.get("lemma") or cand)
            return hit
        try:
            card = _build(cand, ctx)
        except Exception:
            continue
        if not card.get("entries"):
            continue
        with _lock:
            cache[cand] = card
            _save()
        _warm_audio(card.get("lemma") or cand)
        return card
    raise LookupError("没查到：%s" % w)
