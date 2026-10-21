"""考纲词表：判定一个词属于四级词汇、六级词汇还是超纲词。

数据来源：CET-4 4542 词、CET-6 3991 词（去重后并入各自词表，含词性与中文释义）。
词表本身是背单词 App 的词卡集合，不含 repair 这类极基础词，因此再用词频兜底：
不在考纲但属于高频常用词（zipf >= 3.5，约英语前 8000）的，归为基础词而非超纲。
"""
import os

from . import lexicon

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

_CET4, _CET6 = {}, {}
_loaded = False

_FREQ = None
_FREQ_TRIED = False
COMMON_ZIPF = 3.5

# 不规则屈折：词表按原形收录，正文里出现的是变形，不还原就会被误判成超纲词
_IRREG = {
    "held": "hold", "made": "make", "kept": "keep", "done": "do", "lost": "lose",
    "began": "begin", "begun": "begin", "known": "know", "gave": "give", "given": "give",
    "built": "build", "found": "find", "thought": "think", "said": "say", "took": "take",
    "taken": "take", "came": "come", "went": "go", "gone": "go", "got": "get",
    "seen": "see", "saw": "see", "ran": "run", "fell": "fall", "fallen": "fall",
    "broke": "break", "broken": "break", "spoke": "speak", "spoken": "speak",
    "wrote": "write", "written": "write", "drove": "drive", "driven": "drive",
    "chose": "choose", "chosen": "choose", "rose": "rise", "risen": "rise",
    "led": "lead", "fed": "feed", "met": "meet", "paid": "pay", "sold": "sell",
    "told": "tell", "stood": "stand", "understood": "understand", "withdrew": "withdraw",
    "withdrawn": "withdraw", "grew": "grow", "grown": "grow", "drew": "draw",
    "drawn": "draw", "flew": "fly", "flown": "fly", "threw": "throw", "thrown": "throw",
    "wore": "wear", "worn": "wear", "tore": "tear", "torn": "tear", "swam": "swim",
    "sang": "sing", "sung": "sing", "drank": "drink", "drunk": "drink",
    "ate": "eat", "eaten": "eat", "fought": "fight", "bought": "buy", "brought": "bring",
    "caught": "catch", "taught": "teach", "sent": "send", "spent": "spend",
    "lent": "lend", "meant": "mean", "felt": "feel", "left": "leave", "slept": "sleep",
    "dealt": "deal", "heard": "hear", "laid": "lay", "lain": "lie", "sat": "sit",
    "set": "set", "shot": "shoot", "shut": "shut", "split": "split", "spread": "spread",
    "struck": "strike", "sought": "seek", "won": "win", "ran": "run", "hung": "hang",
    "dug": "dig", "hid": "hide", "hidden": "hide", "bit": "bite", "bitten": "bite",
    "blew": "blow", "blown": "blow", "froze": "freeze", "frozen": "freeze",
    "stole": "steal", "stolen": "steal", "shook": "shake", "shaken": "shake",
    "forgot": "forget", "forgotten": "forget", "arose": "arise", "arisen": "arise",
    "beaten": "beat", "bore": "bear", "borne": "bear", "bound": "bind",
}

# 词表以考试词汇为主，日常高频词常有缺漏，这里补齐
_MODERN = {
    "cannot", "email", "online", "offline", "laptop", "podcast", "smartphone",
    "website", "internet", "digital", "software", "hardware", "download", "upload",
    "someone", "somebody", "something", "anything", "everyone", "everything",
    "nobody", "nothing", "anyone", "several", "own", "such", "same", "other",
    "another", "much", "many", "more", "most", "less", "least", "few", "little",
    "always", "never", "often", "sometimes", "usually", "already", "still",
    "almost", "enough", "quite", "rather", "very", "too", "also", "even", "just",
    "only", "again", "once", "twice", "together", "instead", "however", "therefore",
    "meanwhile", "otherwise", "moreover", "nevertheless", "consequently",
    "whether", "although", "though", "unless", "while", "whereas", "because",
    "since", "until", "during", "despite", "throughout", "within", "without",
    "across", "beyond", "towards", "toward", "among", "amongst", "against",
    "around", "behind", "beside", "between", "beneath", "beneath", "upon",
    "onto", "into", "over", "under", "above", "below", "after", "before",
    "right", "left", "sense", "scene", "way", "thing", "stuff", "kind", "sort",
    "lot", "bit", "part", "piece", "number", "group", "whole", "half", "side",
}


def _load():
    global _loaded
    if _loaded:
        return
    for fname, target in (("cet4.tsv", _CET4), ("cet6.tsv", _CET6)):
        path = os.path.join(_ROOT, "data", fname)
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("#"):
                        continue
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 3:
                        continue
                    w = parts[0].strip().lower()
                    if w:
                        target[w] = (parts[1].strip(), parts[2].strip())
        except FileNotFoundError:
            pass
    _loaded = True


def _zipf(word):
    global _FREQ, _FREQ_TRIED
    if _FREQ is None and not _FREQ_TRIED:
        _FREQ_TRIED = True
        try:
            from wordfreq import zipf_frequency
            _FREQ = zipf_frequency
        except Exception:
            _FREQ = False
    if not _FREQ:
        return None
    try:
        return _FREQ(word, "en")
    except Exception:
        return None


def _forms(w):
    """词形候选：原形、去复数/时态/比较级、加 e、去重字母。"""
    yield w
    if w in _IRREG:
        yield _IRREG[w]
    for suf, rep in (("ies", "y"), ("ied", "y"), ("ier", "y"), ("iest", "y"),
                     ("ying", "y"), ("es", ""), ("ed", ""), ("ing", ""),
                     ("er", ""), ("est", ""), ("s", ""), ("d", ""),
                     ("ly", ""), ("ally", "al"), ("ity", "e")):
        if w.endswith(suf) and len(w) - len(suf) >= 2:
            base = w[:-len(suf)] + rep
            yield base
            if suf in ("ed", "ing", "er", "est", "es", "s"):
                yield base + "e"
                if len(base) > 2 and base[-1] == base[-2]:
                    yield base[:-1]
            if suf == "ly":
                yield base + "y"
                yield base + "le"


def lookup(word: str):
    """返回 (tier, pos, gloss)。

    tier: "cet4" | "cet6" | "common" | "beyond"
    """
    _load()
    w = word.lower().strip("-'")
    if not w:
        return "common", "", ""
    for cand in _forms(w):
        if cand in _CET4:
            pos, gloss = _CET4[cand]
            return "cet4", pos, gloss
    for cand in _forms(w):
        if cand in _CET6:
            pos, gloss = _CET6[cand]
            # 六级词表里也混入了部分四级词
            if cand in _CET4:
                return "cet4", _CET4[cand][0], _CET4[cand][1]
            return "cet6", pos, gloss
    for cand in _forms(w):
        if cand in _MODERN or lexicon.is_easy(cand):
            return "common", "", ""
    z = _zipf(w)
    if z is not None and z >= COMMON_ZIPF:
        return "common", "", ""
    return "beyond", "", ""


def lemma(word: str) -> str:
    """返回词典原形（给查词用）；不在词表里就返回本身。

    只认词表能验证的还原，宁可返回本身，也不猜一个不存在的词。
    """
    _load()
    w = (word or "").lower().strip("-'")
    if not w:
        return w
    if w in _CET4 or w in _CET6:
        return w
    for cand in _forms(w):
        if cand != w and (cand in _CET4 or cand in _CET6):
            return cand
    return w


def is_cet6_only(word: str) -> bool:
    return lookup(word)[0] == "cet6"


def pos_of(word: str):
    _load()
    w = word.lower()
    for cand in _forms(w):
        if cand in _CET4:
            return _norm_pos(_CET4[cand][0])
        if cand in _CET6:
            return _norm_pos(_CET6[cand][0])
    return None


def _norm_pos(p):
    if not p:
        return None
    p = p.split("/")[0].strip().lower()
    if p in ("vt", "vi"):
        return "v"
    return p if p in ("n", "v", "adj", "adv") else None


def gloss_of(word: str) -> str:
    return lookup(word)[2]


def stats() -> dict:
    _load()
    return {"cet4": len(_CET4), "cet6": len(_CET6),
            "freq": bool(_FREQ) if _FREQ_TRIED else None}
