"""命题人与编辑的规则。

选材上，六级阅读只认一类稿子：议论或说明文体，一篇一个主张，段落成篇，
有可考的细节与可推的结论。问答稿、直播稿、图集、影评、带货文一律不选——
不是难度问题，是题型无法落到"主旨/细节/推断/态度"四类上。

改稿上，编辑做的是可逆的小手术，不重写内容：
  · 超过 34 词的句子，在连接词处断开（真题很少出现 40 词以上的句子）
  · 一串 8 词以下的短句，合并成一句（否则 FRE 虚高，读起来像口语）
  · 导语里的时间地点交代、记者署名、订阅提示，删掉
断句与合句互为反向操作，两端都能调到目标区间。
"""
import re

from . import difficulty
from .textutil import split_sentences

# 文体不对：这些标题形态对应的是问答、直播、图集、评测、促销
_BAD_TITLE = re.compile(
    r"^(q&a|qa:|inside |how to |what to |why you |review:|hands-on|live|"
    r"podcast|video|photos?:|in pictures|quiz|newsletter|opinion:|editorial:)", re.I)

# 新闻电头、记者署名、订阅尾巴
_JUNK = re.compile(
    r"^(by\s+[A-Z][a-z]+\s+[A-Z][a-z]+|updated?:|published:|image source|credit:|"
    r"subscribe|sign up|follow us|share this|read more|advertisement|"
    r"this article (was|is) (originally )?published)", re.I)

_ARGUMENT_MARKERS = (
    "however", "but ", "although", "therefore", "rather than", "argues", "suggests",
    "in fact", "on the contrary", "as a result", "yet ", "critics", "evidence",
)

_SPLIT_POINTS = [
    "; ", ", and ", ", but ", ", so ", ", yet ", ", which ", ", while ",
    ", although ", ", because ", ", whereas ", " and then ", " — ", ", ",
]
_LEAD_CONJ = re.compile(r"^(and|but|so|yet|which|while|although|because|whereas)\s+", re.I)


def fitness(title, text):
    """命题人视角的选材评分：0–100，低于 55 不建议用。"""
    score, why = 100.0, []
    paras = [p for p in re.split(r"\n{1,}", text) if len(p.split()) >= 12]
    if len(paras) < 3:
        score -= 30
        why.append("段落不成篇")
    avg_para = sum(len(p.split()) for p in paras) / max(1, len(paras))
    if avg_para < 28:
        score -= 18
        why.append("段落过碎（多为快讯）")
    if _BAD_TITLE.match(title or ""):
        score -= 35
        why.append("文体不符（问答/直播/图集）")

    # 引语密度过高 = 访谈稿，主旨题无处落脚
    quotes = text.count('"') + text.count("“") + text.count("”")
    if quotes > 12:
        score -= 20
        why.append("引语过多（访谈体）")

    # 专名过多 = 时政快讯，考生靠常识也能猜
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", text)
    proper = sum(1 for w in words[1:] if w[:1].isupper()) / max(1, len(words))
    if proper > 0.06:
        score -= 15
        why.append("专名密度过高")

    low = text.lower()
    markers = sum(low.count(m) for m in _ARGUMENT_MARKERS)
    if markers < 3:
        score -= 12
        why.append("缺少论证结构")
    else:
        score += min(6, markers)

    return max(0, round(score)), why


def _split_one(sent, max_len):
    if len(sent.split()) <= max_len:
        return None
    for sep in _SPLIT_POINTS:
        idx = sent.find(sep)
        while idx != -1:
            left, right = sent[:idx].strip(), sent[idx + len(sep):].strip()
            if len(left.split()) >= 9 and len(right.split()) >= 7:
                right = _LEAD_CONJ.sub("", right)
                right = right[:1].upper() + right[1:]
                if not left.endswith((".", "?", "!")):
                    left += "."
                return left + " " + right
            idx = sent.find(sep, idx + 1)
    return None


def _split_once(sents, max_len=34):
    for i in sorted(range(len(sents)), key=lambda k: -len(sents[k].split())):
        new = _split_one(sents[i], max_len)
        if new:
            out = list(sents)
            out[i] = new
            return " ".join(out)
    return None


def _merge_once(sents, max_join=34):
    """把相邻的两句短句合成一句，压掉口语式的碎句，抬升平均句长。"""
    best = None
    for i in range(len(sents) - 1):
        a, b = sents[i], sents[i + 1]
        la, lb = len(a.split()), len(b.split())
        if la + lb > max_join:
            continue
        if min(la, lb) > 16:
            continue
        if best is None or la + lb < best[0]:
            best = (la + lb, i)
    if not best:
        return None
    i = best[1]
    a = sents[i].rstrip()
    b = sents[i + 1].strip()
    head = a[:-1] if a.endswith(".") else a
    joined = head + "; " + b[:1].lower() + b[1:]
    return " ".join(sents[:i] + [joined] + sents[i + 2:])


def clean(text):
    """删掉电头、署名与订阅尾巴，保留正文句子。"""
    sents = [s.strip() for s in split_sentences(text) if s.strip()]
    kept = [s for s in sents if not _JUNK.match(s)]
    return " ".join(kept or sents)


def edit(text, kind="reading", rounds=16):
    """按编辑手法把文本调到六级区间。返回 (text, measure, 操作记录)。"""
    text = clean(text)
    log = []
    for _ in range(rounds):
        m = difficulty.analyze(text, kind, ignore=("words",))
        if m["in_band"]:
            break
        sents = split_sentences(text)
        if m["asl"] > 24.8 or m["long_sent"] > 0.36:
            new = _split_once(sents, 34)
            if new:
                text, _ = new, log.append("断句")
                continue
        if m["flesch"] > 53.5 or m["asl"] < 19.0:
            new = _merge_once(sents)
            if new:
                text, _ = new, log.append("合句")
                continue
        if m["flesch"] < 42.0:
            new = _split_once(sents, 28)
            if new:
                text, _ = new, log.append("再断句")
                continue
        break
    return text, difficulty.analyze(text, kind, ignore=("words",)), log
