"""自动校订：把模型稿机械地调进六级区间。

模型一次出稿很难刚好落在区间里（实测约一半不合格），但偏差多是
「长了 50 词」「句子太长」「词太难」这类可量化的偏差，不必重写整篇，
按指标做外科手术即可：删句、断句、合句、换浅词。实在不够长才回头找模型扩写。

每一步都是确定性操作，可以反复迭代，迭代结果再过闸门；仍不达标才留下偏差说明。
"""
import re

from . import difficulty, llm
from .textutil import split_sentences

# 浅词替换：只换虚词化的书面语，不动术语与专名
SIMPLE = {
    "approximately": "about", "utilize": "use", "utilized": "used", "utilizing": "using",
    "numerous": "many", "demonstrate": "show", "demonstrates": "shows", "demonstrated": "showed",
    "commence": "begin", "commenced": "began", "terminate": "end", "terminated": "ended",
    "sufficient": "enough", "obtain": "get", "obtained": "got", "purchase": "buy",
    "purchased": "bought", "assist": "help", "assisted": "helped", "require": "need",
    "requires": "needs", "required": "needed", "additional": "more", "frequently": "often",
    "primarily": "mainly", "consequently": "so", "nevertheless": "but", "furthermore": "also",
    "moreover": "also", "substantial": "large", "individuals": "people", "facilitate": "help",
    "encounter": "meet", "endeavor": "try", "regarding": "about", "initially": "at first",
    "currently": "now", "subsequently": "later", "ultimately": "in the end",
    "in order to": "to", "due to the fact that": "because", "a large number of": "many",
    "at the present time": "now", "in the event that": "if", "prior to": "before",
    "with regard to": "about", "as a consequence of": "because of",
}

_SPLIT_PAT = (", and ", "; ", ", but ", ", so ", ", while ", ", whereas ")


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def _band(kind, key):
    return difficulty.BANDS.get(kind, difficulty.BANDS["reading"])[key]


def _split_longest(text, limit=32):
    """把最长的一句在连接词处断开，降低平均句长。"""
    sents = split_sentences(text)
    if not sents:
        return text, False
    idx = max(range(len(sents)), key=lambda i: len(sents[i].split()))
    s = sents[idx]
    if len(s.split()) <= limit:
        return text, False
    for pat in _SPLIT_PAT:
        p = s.find(pat)
        if p < 25:
            continue
        head = s[:p].rstrip().rstrip(",")
        tail = s[p + len(pat):].strip()
        if len(head.split()) < 8 or len(tail.split()) < 6:
            continue
        tail = tail[:1].upper() + tail[1:]
        sents[idx] = head + ". " + tail
        return " ".join(sents), True
    return text, False


def _merge_pair(text, min_words=13):
    """把最短的相邻两句合成一句，抬高平均句长。"""
    sents = split_sentences(text)
    best = None
    for i in range(len(sents) - 1):
        wa, wb = len(sents[i].split()), len(sents[i + 1].split())
        if wa + wb > 34:
            continue
        if wa > min_words and wb > min_words:
            continue
        if best is None or wa + wb < best[0]:
            best = (wa + wb, i)
    if not best:
        return text, False
    i = best[1]
    a = sents[i].rstrip().rstrip(".").rstrip()
    b = sents[i + 1].strip()
    b = b[:1].lower() + b[1:] if b[:1].isupper() and not b.split()[0].isupper() else b
    sents[i] = a + ", and " + b
    del sents[i + 1]
    return " ".join(sents), True


def _simplify(text):
    """把书面语虚词换成短词，抬 FRE。只换一次，避免来回替换。"""
    out, changed = text, False
    for a, b in SIMPLE.items():
        pat = re.compile(r"\b%s\b" % re.escape(a), re.I)
        if pat.search(out):
            out = pat.sub(b, out)
            changed = True
    return out, changed


def _trim_tail(text, protect=()):
    """从尾部收一句，用于小幅压词数。

    只动最后一段的末句——绝不去掉中间段落里的句子，否则后文指代就悬空了，
    读起来会「不通顺」正是这么来的。末句若是考点定位句，就整篇不再收。
    """
    guarded = [_norm(p)[:70] for p in protect if p]
    paras = [p for p in text.split("\n\n") if p.strip()]
    if not paras:
        return text, False
    while paras:
        sents = split_sentences(paras[-1])
        if not sents:
            paras.pop()
            continue
        last = _norm(sents[-1])
        if any(g and (g in last or last[:60] in g) for g in guarded):
            return text, False
        if len(sents) == 1:
            if len(paras) == 1:
                return text, False
            paras.pop()
        else:
            sents.pop()
            paras[-1] = " ".join(sents)
        return "\n\n".join(paras), True
    return text, False


def _condense_by_llm(settings, text, kind, target):
    """超长时让模型压缩，而不是删句：保留全部事实与逻辑，只压缩冗余表达。"""
    if not settings or not llm.is_configured(settings):
        return text, False
    prompt = (
        "下面这段英文超过目标篇幅。请压缩到约 %d 词：\n"
        "1. 保留全部事实、论证与段落结构，不要删掉任何一段，不要改变因果关系；\n"
        "2. 只压缩冗余的修饰、重复的举例与可省的从句，保证读起来连贯；\n"
        "3. 保持六级难度（平均句长 20–24 词）；\n"
        "4. 只输出压缩后的正文，不要标题、不要解释。\n\n%s" % (target, text)
    )
    try:
        out = _norm(llm.chat(settings, [{"role": "user", "content": prompt}],
                             temperature=0.4, max_tokens=2400))
        if len(out.split()) >= 60:
            return out, True
    except Exception:
        pass
    return text, False


def _extend_by_llm(settings, text, kind, target):
    """词数不足时让模型扩写（最多一次），失败返回原文。"""
    if not settings or not llm.is_configured(settings):
        return text, False
    prompt = (
        "把下面这段英文扩写到约 %d 词，保持原意、原结构与六级难度（平均句长 18–24 词）。"
        "只输出扩写后的正文，不要标题、不要解释。\n\n%s" % (target, text)
    )
    try:
        out = llm.chat(settings, [{"role": "user", "content": prompt}],
                       temperature=0.5, max_tokens=1600)
        out = _norm(out)
        if len(out.split()) >= len(text.split()) * 0.95:
            return out, True
    except Exception:
        pass
    return text, False


def _repair_plain(text, kind, settings, protect=(), extend_target=None):
    """对一段连续文本反复做手术，直到进带或再也动不了。"""
    logs = []
    rewritten = False
    condensed = False
    for _ in range(14):
        m = difficulty.analyze(text, kind)
        if m["in_band"]:
            return text, logs
        changed = False

        if m["words"] > _band(kind, "words")[3]:
            hi = _band(kind, "words")[3]
            if m["words"] > hi * 1.10 and not condensed:
                condensed = True
                target = int(sum(_band(kind, "words")[1:3]) / 2)
                new_text, ok = _condense_by_llm(settings, text, kind, target)
                if ok:
                    logs.append("模型压缩（%d→%d 词，保留全部段落）" % (
                        len(text.split()), len(new_text.split())))
                    text = new_text
                    changed = True
            if not changed:
                text, ok = _trim_tail(text, protect)
                if ok:
                    logs.append("尾部收一句（%d→%d 词）" % (m["words"], len(text.split())))
                    changed = True

        if m["words"] < _band(kind, "words")[0] and extend_target:
            before = len(text.split())
            text, ok = _extend_by_llm(settings, text, kind, extend_target)
            if ok:
                logs.append("模型扩写（%d→%d 词）" % (before, len(text.split())))
                changed = True
                extend_target = None      # 只扩一次

        if m["asl"] > _band(kind, "asl")[2]:
            text, ok = _split_longest(text)
            changed |= ok

        if m["asl"] < _band(kind, "asl")[1]:
            text, ok = _merge_pair(text)
            changed |= ok

        if m["flesch"] < _band(kind, "flesch")[1]:
            text, ok = _simplify(text)
            if ok:
                logs.append("浅化书面词")
                changed = True

        if not changed:
            if not rewritten and not m["in_band"]:
                rewritten = True
                target = int(sum(_band(kind, "words")[1:3]) / 2)
                fre = float(sum(_band(kind, "flesch")[1:3]) / 2)
                new_text, ok = _rewrite_by_llm(settings, text, kind, target, fre)
                if ok and new_text.strip() and new_text.strip() != text.strip():
                    # 只有确实更接近区间才采纳。以前是「写完就换」，
                    # 结果把 537 词的压缩成果改成 560 词、FRE 反而更低——
                    # 越修越偏，最后整套被判「难度差距大」。
                    nm = difficulty.analyze(new_text, kind)
                    if nm["in_band"] and not m["in_band"] or nm["score"] > m["score"]:
                        logs.append("模型按指标重写（%d→%d 词，%d 分）" % (
                            len(text.split()), len(new_text.split()), nm["score"]))
                        text = new_text
                        continue
                    logs.append("模型重写未改善（%d→%d 词，%d 分），弃用" % (
                        len(text.split()), len(new_text.split()), nm["score"]))
            break
    return text, logs


def _reading_evidence(sec):
    return [q.get("evidence", "") for q in (sec.get("questions") or []) if q.get("evidence")]


def _repair_conversation(sec, settings):
    """对话按「行」处理：超长删尾部轮次，句子长就在行内断句，词太难就浅化。"""
    logs = []
    script = sec.get("script") or []
    if not script:
        return logs
    kind = "long_conversation"
    lo, hi = _band(kind, "words")[0], _band(kind, "words")[3]

    def total():
        return sum(len(l.get("en", "").split()) for l in script)

    # 1) 超长：保住开头铺垫与最后一句收束，只删中间一整轮，避免结尾被砍断
    guarded = {0, 1, len(script) - 1}
    for i, l in enumerate(script):
        if any(ev and ev in l["en"] for ev in _reading_evidence(sec)):
            guarded.add(i)
    guard = 0
    while total() > hi and len(script) > 8 and guard < 12:
        guard += 1
        best = None
        for i in range(2, len(script) - 2):
            if i in guarded or i + 1 in guarded:
                continue
            w = len(script[i]["en"].split()) + len(script[i + 1]["en"].split())
            if best is None or w > best[0]:
                best = (w, i)
        if not best:
            break
        before = total()
        del script[best[1]:best[1] + 2]
        logs.append("删去中间一轮对话（%d→%d 词）" % (before, total()))

    # 2) 句子太长：在行内断句
    guard = 0
    while guard < 8:
        guard += 1
        text = " ".join(l["en"] for l in script)
        m = difficulty.analyze(text, kind)
        if m["asl"] <= _band(kind, "asl")[2]:
            break
        idx = max(range(len(script)), key=lambda i: len(script[i]["en"].split()))
        new, ok = _split_longest(script[idx]["en"])
        if not ok:
            break
        script[idx]["en"] = new

    # 3) 词太难：逐行浅化
    text = " ".join(l["en"] for l in script)
    if difficulty.analyze(text, kind)["flesch"] < _band(kind, "flesch")[1]:
        for l in script:
            l["en"], ok = _simplify(l["en"])
        logs.append("对话浅化用词")

    sec["script"] = script
    m = difficulty.analyze(" ".join(l["en"] for l in script), kind)
    logs.append("对话校订后：%d 词 / 句长 %.1f / FRE %.1f / %s" % (
        m["words"], m["asl"], m["flesch"], "达标" if m["in_band"] else "仍偏"))
    return logs


def _repair_passage(sec, settings):
    script = sec.get("script") or []
    if not script:
        return []
    text = " ".join(l.get("en", "") for l in script)
    new, logs = _repair_plain(text, "passage_listening", settings, extend_target=320)
    sec["script"] = [{"sp": script[0].get("sp", "N"), "en": new}]
    fixed = _refit_evidence(sec, new)
    if fixed:
        logs.append("篇章重定位 %d 题" % len(fixed))
    m = difficulty.analyze(new, "passage_listening")
    logs.append("篇章校订后：%d 词 / 句长 %.1f / FRE %.1f / %s" % (
        m["words"], m["asl"], m["flesch"], "达标" if m["in_band"] else "仍偏"))
    return logs


def _repair_reading(sec, settings):
    text = sec.get("passage") or ""
    if not text.strip():
        return []
    protect = _reading_evidence(sec)
    new, logs = _repair_plain(text, "reading", settings, protect=protect, extend_target=440)
    # 删改后确认定位句仍是正文子串，否则退回原句所在段
    fixed = _refit_evidence(sec, new)
    sec["passage"] = new
    m = difficulty.analyze(new, "reading")
    logs.append("阅读校订后：%d 词 / 句长 %.1f / FRE %.1f / %s" % (
        m["words"], m["asl"], m["flesch"], "达标" if m["in_band"] else "仍偏"))
    if fixed:
        logs.append("重定位 %d 题：%s" % (len(fixed), "、".join(x for x in fixed if x)))
    return logs


def apply(sections, settings=None):
    """对整套题逐板块校订，返回操作日志。"""
    logs = []
    for sec in sections:
        t = sec.get("type")
        try:
            if t == "long_conversation":
                logs += _repair_conversation(sec, settings)
            elif t == "passage_listening":
                logs += _repair_passage(sec, settings)
            elif t == "reading":
                logs += _repair_reading(sec, settings)
        except Exception as e:
            logs.append("%s 校订出错：%r" % (t, e))
    return logs

def _refit_evidence(sec, text):
    """校订后重新对齐定位句，保证仍是正文逐字子串。"""
    flat = _norm(text)
    changed = []
    for q in sec.get("questions") or []:
        ev = _norm(q.get("evidence"))
        if not ev or ev in flat:
            continue
        keys = [w for w in re.findall(r"[A-Za-z]{5,}", ev)][:5]
        best, hit = "", 0
        for st in split_sentences(flat):
            n = sum(1 for w in keys if w.lower() in st.lower())
            if n > hit:
                best, hit = st, n
        if hit >= 2:
            q["evidence"] = best.strip()
            changed.append(q.get("id"))
    return changed


def _rewrite_by_llm(settings, text, kind, words_target, fre_target):
    """机械手术救不回来时，让模型按指标重写一遍（保持事实不变）。"""
    if not settings or not llm.is_configured(settings):
        return text, False
    prompt = (
        "下面这段英文对大学英语六级读者偏难。请重写一遍，要求：\n"
        "1. 保持所有事实、人名、地名、数字、因果关系不变；\n"
        "2. 把长句拆短，平均句长约 %d 词；\n"
        "3. 把生僻词换成常见词，使 Flesch Reading Ease 约 %.0f；\n"
        "4. 总词数约 %d 词；\n"
        "5. 只输出重写后的英文正文，不要标题、不要解释。\n\n%s"
        % (int(_band(kind, "asl")[2] - 3), fre_target, words_target, text)
    )
    try:
        out = _norm(llm.chat(settings, [{"role": "user", "content": prompt}],
                             temperature=0.5, max_tokens=2200))
        if len(out.split()) >= 80:
            return out, True
    except Exception:
        pass
    return text, False
