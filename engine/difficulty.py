"""CET-6 难度标尺。

区间依据（真题语料实测，非估计值）：
  · 2007–2017 六级阅读语篇 65710 词：FRE 49.7、超纲词率 13.6%、平均词长 5.0
  · 2020–2022 六级阅读 45 篇：FRE 均值 41.73（SD 7.71，极差 24.0–61.1）、
    阅读指数 FKGL 均值 13.93
  · 杨惠中（2004）历年六级：FRE 49.1；四级 57.7
因此仔细阅读的可接受带为 FRE 40–55（中心 47），听力另设更宽的带。

板块之间不共用一套阈值：听力是口语体，句长天然短于书面阅读，
用阅读的尺子去量听力只会把对话改成朗诵稿。
"""
import re

from . import vocab
from .textutil import split_sentences, syllables, words

# 虚词与极高频功能词：计入基础词，不算难度词，也不算超纲
FUNCTION = set("""
a an the of to in on at by for with from as into onto upon within without through
during between among against about above below under over after before since until
while when where why how what which who whom whose that this these those there here
and or but nor so yet if then than because although though unless whether either neither
both each every all any some no not none one two three four five six seven eight nine ten
i you he she it we they me him her us them my your his its our their mine yours hers ours
is am are was were be been being have has had do does did will would shall should can
could may might must ought
also just only even still very too quite rather almost already always never often
sometimes usually again once twice much many more most less least few little several
other another such same own up down out off away back forth ahead apart together
""".split())

# 板块难度带：(下限, 下限舒适区, 上限舒适区, 上限)
BANDS = {
    "reading": {
        "words":     (395, 415, 455, 480),
        "asl":       (18.0, 20.0, 26.0, 27.5),
        "flesch":    (38.0, 41.0, 55.0, 58.0),
        "fkgl":      (10.5, 12.0, 16.0, 17.0),
        "long_sent": (0.05, 0.10, 0.38, 0.46),
        "beyond":    (0.00, 0.02, 0.14, 0.18),
        "word_len":  (4.6, 4.75, 5.35, 5.6),
    },
    "banked_cloze": {
        "words":     (250, 265, 315, 340),
        "asl":       (17.0, 19.0, 25.0, 26.5),
        "flesch":    (38.0, 41.0, 55.0, 58.0),
        "fkgl":      (10.0, 11.0, 16.0, 17.0),
        "long_sent": (0.05, 0.10, 0.36, 0.45),
        "beyond":    (0.00, 0.02, 0.14, 0.18),
        "word_len":  (4.6, 4.75, 5.35, 5.6),
    },
    "long_conversation": {
        "words":     (285, 300, 365, 395),
        "asl":       (10.5, 12.0, 16.5, 18.5),
        "flesch":    (60.0, 62.0, 76.0, 80.0),
        "fkgl":      (6.0, 7.0, 11.0, 12.5),
        "long_sent": (0.02, 0.06, 0.26, 0.36),
        "beyond":    (0.00, 0.02, 0.10, 0.14),
        "word_len":  (4.3, 4.45, 4.95, 5.15),
    },
    "passage_listening": {
        "words":     (275, 290, 345, 375),
        "asl":       (15.0, 17.0, 23.0, 25.5),
        "flesch":    (52.0, 55.0, 67.0, 71.0),
        "fkgl":      (8.5, 9.5, 14.0, 15.5),
        "long_sent": (0.04, 0.10, 0.32, 0.42),
        "beyond":    (0.00, 0.02, 0.12, 0.16),
        "word_len":  (4.5, 4.6, 5.2, 5.4),
    },
}

# 容差：略微偏难/偏易可接受，只扣分不判失败（命题人也不会篇篇卡在均值上）
#   比例型（words/beyond/long_sent）按相对值，其余按绝对差
TOL = {"words": 0.05, "asl": 1.2, "flesch": 2.5, "fkgl": 1.0,
       "long_sent": 0.04, "beyond": 0.03, "word_len": 0.12}
_REL = {"words", "long_sent", "beyond"}

# 软指标：只影响评分，不参与达标判定
SOFT = {"long_sent", "fkgl", "word_len"}  # 与 FRE 同源，重复设门槛会双重扣分

_CN = {"words": "词数", "asl": "平均句长", "flesch": "FRE", "fkgl": "FKGL",
       "long_sent": "长句占比", "beyond": "超纲词率", "word_len": "平均词长"}

_PROPER = re.compile(r"^[A-Z][a-z]+$")


def _proper_nouns(text):
    """句首大写不算专名；只认句中大写且非句首的词。"""
    out = set()
    for sent in split_sentences(text):
        ws = words(sent)
        for w in ws[1:]:
            if _PROPER.match(w):
                out.add(w.lower())
    return out


def analyze(text: str, kind: str = "reading", ignore=()) -> dict:
    sents = split_sentences(text)
    ws = words(text)
    nw, ns = max(1, len(ws)), max(1, len(sents))
    syl = sum(syllables(w) for w in ws)
    asl, asw = nw / ns, syl / nw
    fre = 206.835 - 1.015 * asl - 84.6 * asw
    fkgl = 0.39 * asl + 11.8 * asw - 15.59

    long_sents = sum(1 for s in sents if len(words(s)) >= 30)
    proper = _proper_nouns(text)
    beyond, cet6_words = [], []
    for w in ws:
        lw = w.lower()
        if lw in FUNCTION or lw in proper:
            continue
        tier = vocab.lookup(lw)[0]
        if tier == "beyond":
            beyond.append(lw)
        elif tier == "cet6":
            cet6_words.append(lw)
    wlen = sum(len(w) for w in ws) / nw

    m = {
        "words": nw, "sentences": ns,
        "asl": round(asl, 1), "flesch": round(fre, 1), "fkgl": round(fkgl, 1),
        "long_sent": round(long_sents / ns, 3),
        "beyond": round(len(beyond) / nw, 3),
        "cet6_density": round(len(cet6_words) / nw, 3),
        "word_len": round(wlen, 2),
        "beyond_words": sorted(set(beyond))[:12],
        "cet6_words": sorted(set(cet6_words))[:12],
    }
    return _verdict(m, kind, ignore)



def _within_tol(key, v, bound):
    """越界是否仍在「略微偏难/偏易」的可接受范围内。"""
    t = TOL.get(key)
    if not t:
        return False
    if key in _REL:
        return abs(v - bound) <= abs(bound) * t
    return abs(v - bound) <= t


def _verdict(m, kind, ignore=()):
    band = BANDS.get(kind) or BANDS["reading"]
    issues, notes, score = [], [], 100.0
    for key, (lo, lo_ok, hi_ok, hi) in band.items():
        if key in ignore:
            continue
        if key in SOFT:
            v = m[key]
            lo_s, _, hi_s, _ = band[key]
            if v < lo_s or v > hi_s:
                score -= 6
            continue
        v = m[key]
        if (v < lo or v > hi) and _within_tol(key, v, lo if v < lo else hi):
            notes.append("%s %.2f 略%s（在容差内，可接受）" % (_CN[key], v, "低于下限" if v < lo else "超出上限"))
            score -= 7
            continue
        if v < lo:
            issues.append("%s %.2f 低于下限 %.2f" % (_CN[key], v, lo))
            score -= min(30, (lo - v) / max(1e-6, lo_ok - lo) * 22 + 8)
        elif v < lo_ok:
            notes.append("%s %.2f 略低于舒适区 %.2f" % (_CN[key], v, lo_ok))
            score -= (lo_ok - v) / max(1e-6, lo_ok - lo) * 7
        elif v <= hi_ok:
            pass
        elif v <= hi:
            # 只是离开舒适区、仍在硬区间内：扣分但不判失败
            notes.append("%s %.2f 略高于舒适区 %.2f" % (_CN[key], v, hi_ok))
            score -= (v - hi_ok) / max(1e-6, hi - hi_ok) * 9
        else:
            issues.append("%s %.2f 超出上限 %.2f" % (_CN[key], v, hi))
            score -= min(30, (v - hi) / max(1e-6, hi - hi_ok) * 22 + 8)
    m["score"] = max(0, round(score))
    m["in_band"] = not issues
    m["issues"] = issues
    m["notes"] = notes
    m["kind"] = kind
    return m


def check_set(data: dict) -> dict:
    """整套题逐板块过尺子，返回总判定。"""
    reports = []
    for s in data.get("sections", []):
        t = s.get("type")
        if t in ("long_conversation", "passage_listening"):
            text = " ".join(l.get("en", "") for l in s.get("script", []))
        else:
            text = s.get("passage", "")
        if t == "banked_cloze" and s.get("answers"):
            # 挖空会削掉全文最难的词，测出来必然偏易；按答案还原后再量
            for a in s["answers"]:
                text = text.replace("[%d]" % a["index"], a["word"], 1)
        if not text.strip():
            continue
        r = analyze(text, t)
        r["id"], r["type"], r["title"] = s.get("id"), t, s.get("title", "")
        reports.append(r)

    avg = round(sum(r["score"] for r in reports) / max(1, len(reports)))
    return {
        "sections": reports,
        "score": avg,
        "in_band": all(r["in_band"] for r in reports),
        "level": _label(avg),
    }


def _label(score):
    if score >= 88:
        return "六级"
    if score >= 74:
        return "接近六级"
    if score >= 60:
        return "偏易（四级水平）"
    return "明显偏易"
