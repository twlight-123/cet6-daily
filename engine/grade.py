"""难度评估：统一走 difficulty 的六级标尺。

早先这里用一套宽松阈值（FRE 28–62 都算「六级」），结果抓来的文章普遍偏易。
现在与阅读板块共用同一条实测带；选材时忽略总词数，因为原文动辄八九百词，
截取之后才进入 400–450 词的区间。
"""
from . import difficulty
from .textutil import split_sentences, syllables, words


def measure(text: str) -> dict:
    sents = split_sentences(text)
    ws = words(text)
    nw = max(1, len(ws))
    ns = max(1, len(sents))
    syl = sum(syllables(w) for w in ws)
    asl = nw / ns
    asw = syl / nw
    fre = 206.835 - 1.015 * asl - 84.6 * asw
    fk = 0.39 * asl + 11.8 * asw - 15.59

    # 选材阶段不看词数，只看句式与词汇难度
    d = difficulty.analyze(text, "reading", ignore=("words",))

    return {
        "words": nw, "sentences": ns,
        "avg_sentence_len": round(asl, 1),
        "flesch_ease": round(fre, 1),
        "flesch_grade": round(fk, 1),
        "hard_word_ratio": d["beyond"],
        "long_sent_ratio": d["long_sent"],
        "level": _level(d),
        "in_band": d["in_band"],
        "distance": _distance(d),
        "issues": d["issues"],
        "score": d["score"],
    }


def _distance(d):
    """离六级带的距离，0 表示命中。选材时用它排序，避免"全不合规就无稿可用"。"""
    lo, hi = 40.0, 57.0
    fre = d["flesch"]
    gap = 0.0 if lo <= fre <= hi else (lo - fre if fre < lo else fre - hi)
    asl = d["asl"]
    if asl < 18:
        gap += (18 - asl) * 0.4
    elif asl > 27:
        gap += (asl - 27) * 0.4
    return round(gap, 2)


def _level(d):
    if d["in_band"]:
        return "六级"
    if d["flesch"] > 57:
        return "偏易"
    if d["flesch"] < 40:
        return "偏难"
    return "接近六级"
