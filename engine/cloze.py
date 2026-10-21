"""本地「15 选 10」生成引擎（无需大模型）。

三条硬约束，对应真实六级选词填空：
  1. 考点必须是考纲内、且达到六级量级的实词（优先六级词表，四级基础词不作考点）；
  2. 十个空的词性要配平——真实试卷大致是名 3–4、动 3–4、形 2–3、副 1–2，
     如果十个空全是形容词，学生不看文章也能靠词性排除；
  3. 干扰项与答案同词性、长度相近，且取自六级词表，不能一眼看出是凑数的。
"""
import random
import re

from . import lexicon, vocab
from .textutil import split_sentences

_TOKEN = re.compile(r"[A-Za-z][A-Za-z'\-]*")

# 每个空的词性配额（按 n 个空等比缩放）。副词不作考点：-ly 词性数据不可靠，
# 而且十个空里放一个副词，学生靠词性就能排除。
_QUOTA = {"n": 0.35, "v": 0.40, "adj": 0.25}

_BAN = {
    "including", "regarding", "concerning", "according", "following", "during", "despite",
    "providing", "depending", "excluding", "assuming", "considering", "beginning",
    "something", "anything", "everything", "nothing", "themselves", "ourselves",
    "whatever", "whenever", "wherever", "without", "within", "through", "between",
    "against", "because", "another", "however", "therefore", "moreover", "nevertheless",
    "consequently", "meanwhile", "otherwise", "altogether", "throughout", "although",
    "cannot", "towards", "toward", "against", "already", "perhaps", "several",
}


def _norm_pos(p):
    if not p:
        return None
    p = p.split("/")[0].strip().lower()
    if p in ("vt", "vi"):
        return "v"
    return p if p in ("n", "v", "adj", "adv") else None


def _pos_of(w):
    """词性以考纲词表为准；词表把兼类词只标一个义项时，按词尾纠正。"""
    if w.endswith("ly") and len(w) > 5:
        return "adv"
    p = _norm_pos(vocab.pos_of(w)) or _norm_pos(lexicon.pos_strict(w))
    # unoccupied / unexpected：否定前缀 + 已知词干的分词，整体是形容词
    for pre in ("un", "non"):
        if w.startswith(pre) and w.endswith("ed") and len(w) > len(pre) + 5:
            if vocab.lookup(w[len(pre):])[0] != "beyond":
                return "adj"
    # 词表偶有把名词标成动词的条目（texture），用后缀拉回来
    if p == "v" and w.endswith(("ure", "tion", "sion", "ment", "ity", "ness", "ance",
                                "ence", "ship", "ism")):
        return "n"
    # conducted / demonstrated 这类被词表读成名词的过去式，正文里是谓语
    if w.endswith("ed") and len(w) > 6 and p in (None, "n"):
        return "v"
    if p is None:
        if w.endswith(("tion", "sion", "ment", "ity", "ance", "ence", "ness", "ship")):
            return "n"
        if w.endswith(("ous", "ive", "able", "ible", "ical", "ful", "less")):
            return "adj"
    return p


def _tokens_with_span(text):
    return [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(text)]


_ADVANCED_SUF = ("tion", "sion", "ment", "ity", "ance", "ence", "ive", "ous",
                 "able", "ible", "ism", "ship", "ify", "ize", "ise")


def _difficulty(w, pos, tier):
    score = len(w) * 0.45
    if tier == "cet6":
        score += 3.2
    elif tier == "beyond":
        score += 2.2
    else:
        score -= 1.6          # 四级基础词：能不用就不用
    if w.endswith(_ADVANCED_SUF):
        score += 1.5          # 派生词通常是六级量级的抽象名词
    if pos == "adv":
        score -= 1.2          # 副词考点少，且 -ly 词性数据不可靠
    return score


def _candidates(text):
    toks = _tokens_with_span(text)
    lower_counts = {}
    for w, _, _ in toks:
        lower_counts[w.lower()] = lower_counts.get(w.lower(), 0) + 1
    sents = split_sentences(text)
    head_len = len(sents[0].split()) if sents else 0

    cands = []
    seen_stem = set()
    for idx, (w, s, e) in enumerate(toks):
        lw = w.lower().strip("-'")
        if len(lw) < 6 or "'" in lw or "-" in lw:
            continue
        if lw in _BAN or lexicon.is_easy(lw):
            continue
        if lower_counts[lw] > 2:
            continue
        if w[0].isupper() and idx > 3:      # 句中大写：专名
            continue
        tier = vocab.lookup(lw)[0]
        if tier == "common":
            continue
        pos = _pos_of(lw)
        if pos is None:
            continue
        if pos == "adv":
            continue                       # 副词不作考点
        if tier == "cet4" and len(lw) < (7 if _pos_of(lw) == "v" else 8):
            continue                       # 四级词太浅，撑不起六级考点
        if lw.endswith("ing"):
            continue          # -ing 形式在正文里多为分词，歧义太大
        if head_len and idx < head_len * 0.7:
            continue
        stem = lw[: max(4, len(lw) - 3)]
        if stem in seen_stem:
            continue
        seen_stem.add(stem)
        cands.append({
            "word": w, "lower": lw, "start": s, "end": e, "token_index": idx,
            "pos": pos, "tier": tier, "score": _difficulty(lw, pos, tier),
        })
    return cands, toks


def _quota_for(n):
    q, acc = {}, 0
    for pos, share in _QUOTA.items():
        k = max(1, round(n * share))
        q[pos] = k
        acc += k
    # 配额总数对齐 n：多退少补，优先砍副词
    order = ["adj", "n", "v"]
    while acc > n:
        for pos in order:
            if acc <= n:
                break
            if q[pos] > 1:
                q[pos] -= 1
                acc -= 1
    while acc < n:
        q["v"] += 1
        acc += 1
    return q


def _pick(cands, toks, n, rnd):
    """先按词性配额在各区选，缺口再全局补，最后才放宽。"""
    total = len(toks)
    quota = _quota_for(n)
    picked = []
    zone = max(1, total // n)
    taken = []          # 暂时放不下的分区
    min_gap = 8

    for i in range(n):
        lo = i * zone
        hi = (i + 1) * zone if i < n - 1 else total
        pool = [c for c in cands
                if lo <= c["token_index"] < hi
                and quota.get(c["pos"], 0) > 0
                and all(abs(c["token_index"] - p["token_index"]) >= min_gap for p in picked)]
        if not pool:
            taken.append(i)
            continue
        pool.sort(key=lambda c: -c["score"])
        choice = pool[rnd.randint(0, min(1, len(pool) - 1))]
        quota[choice["pos"]] = quota.get(choice["pos"], 0) - 1
        picked.append(choice)

    # 第二轮：欠账的分区，从还有配额且间距够的候选里全局补
    for _ in taken:
        if len(picked) >= n:
            break
        pool = [c for c in cands
                if quota.get(c["pos"], 0) > 0
                and all(abs(c["token_index"] - p["token_index"]) >= min_gap for p in picked)]
        if not pool:
            pool = [c for c in cands
                    if all(abs(c["token_index"] - p["token_index"]) >= min_gap for p in picked)]
        if not pool:
            break
        pool.sort(key=lambda c: -c["score"])
        choice = pool[rnd.randint(0, min(1, len(pool) - 1))]
        quota[choice["pos"]] = quota.get(choice["pos"], 0) - 1
        picked.append(choice)

    # 第三轮：还差几个，完全放宽间距
    if len(picked) < n:
        pool = [c for c in cands if c not in picked]
        pool.sort(key=lambda c: -c["score"])
        for c in pool:
            if len(picked) >= n:
                break
            picked.append(c)
    return picked[:n]


def _distractors(answers, text, need, rnd):
    """同词性、长度相近的六级词，保证干扰项看起来像真答案。"""
    used = {a["lower"] for a in answers} | {w.lower() for w in _TOKEN.findall(text)}
    by_pos = {}
    for w, (pos, _gloss) in vocab._CET6.items():
        p = _norm_pos(pos)
        if p and 6 <= len(w) <= 14:
            by_pos.setdefault(p, []).append(w)
    for lst in by_pos.values():
        rnd.shuffle(lst)

    out = []
    for a in answers:
        if len(out) >= need:
            break
        pool = by_pos.get(a["pos"], [])
        target = len(a["word"])
        for w in pool:
            if w in used or abs(len(w) - target) > 3:
                continue
            if any(w[:4] == x["lower"][:4] for x in answers):
                continue
            used.add(w)
            out.append({"word": w, "pos": a["pos"]})
            break
    # 不够就用其他词性补齐
    if len(out) < need:
        rest = [w for lst in by_pos.values() for w in lst if w not in used]
        rnd.shuffle(rest)
        for w in rest:
            if len(out) >= need:
                break
            used.add(w)
            out.append({"word": w, "pos": _pos_of(w)})
    return out


def build(text, n=10, seed=None):
    rnd = random.Random(seed)
    cands, toks = _candidates(text)
    if len(cands) < n:
        return None

    picked = _pick(cands, toks, n, rnd)
    if len(picked) < n:
        return None
    picked.sort(key=lambda c: c["token_index"])

    out, cursor, answers = [], 0, []
    for i, c in enumerate(picked, 1):
        out.append(text[cursor:c["start"]])
        out.append("[%d]" % i)
        cursor = c["end"]
        answers.append({"index": i, "word": c["word"], "lower": c["lower"], "pos": c["pos"],
                        "tier": c["tier"]})
    out.append(text[cursor:])
    passage = "".join(out)

    distractors = _distractors(answers, text, 15 - n, rnd)

    bank_words = sorted([a["word"] for a in answers] + [d["word"] for d in distractors],
                        key=lambda w: w.lower())
    letters = "ABCDEFGHIJKLMNO"
    bank = [{"label": letters[i], "word": w} for i, w in enumerate(bank_words)]
    label_of = {b["word"]: b["label"] for b in bank}
    for a in answers:
        a["label"] = label_of[a["word"]]

    from collections import Counter
    return {
        "passage": passage,
        "answers": answers,
        "bank": bank,
        "stats": {"candidates": len(cands), "blanks": n,
                  "pos": dict(Counter(a["pos"] for a in answers))},
    }
