"""题目质量闸门：按六级命题规范逐项检查，不合格的退给模型重做。

检查的是「命题」而不是「文本难度」——难度由 difficulty.py 管，这里管题干、选项、答案、解析、定位句。
规则来自六级真题的客观特征：
- 每题四个选项、同一题内结构一致、长度接近；
- 正确答案不能总落在同一个字母，也不能连续三题相同；
- 题干简短，否定问法一节课最多一题；
- 解析要中文，且逐个说明干扰项为什么错；
- 定位句必须是原文逐字子串（听力为原台词）。
"""
import re
from collections import Counter

WANT = {"long_conversation": 4, "passage_listening": 3, "reading": 5}
NEG = re.compile(r"\b(NOT|EXCEPT|least|incorrect|false)\b", re.I)
CN = re.compile(r"[\u4e00-\u9fff]")


def _wc(s):
    return len((s or "").split())


def _flat(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def _text_of(sec):
    if sec.get("passage"):
        return _flat(sec["passage"])
    return _flat(" ".join(l.get("en", "") for l in (sec.get("script") or [])))


def check_section(sec):
    """返回问题清单；空列表表示这一节合格。"""
    issues = []
    kind = sec.get("type")
    qs = sec.get("questions") or []
    want = WANT.get(kind)
    if want and len(qs) != want:
        issues.append("%s 题数为 %d，应为 %d" % (kind, len(qs), want))
    if not qs:
        return issues

    # 答案分布
    ans = []
    for q in qs:
        try:
            ans.append(int(q.get("answer", -1)))
        except Exception:
            ans.append(-1)
    if any(a < 0 or a > 3 for a in ans):
        issues.append("有答案索引越界（应为 0–3）")
    if len(qs) >= 4:
        c = Counter(ans)
        if len(c) < 3:
            issues.append("答案字母只覆盖 %d 种（应 ≥3 种）" % len(c))
        if max(c.values()) > 2:
            issues.append("同一字母作答案 %d 次（上限 2）" % max(c.values()))
        for i in range(len(ans) - 2):
            if ans[i] == ans[i + 1] == ans[i + 2]:
                ids = "/".join(q.get("id", "?") for q in qs[i:i + 3])
                issues.append("连续三题答案相同（%s）" % ids)

    text = _text_of(sec)
    neg = 0
    for q in qs:
        qid = q.get("id", "?")
        opts = q.get("options") or []
        if len(opts) != 4:
            issues.append("%s 选项数为 %d（应为 4）" % (qid, len(opts)))
            continue
        opts = [str(o).strip() for o in opts]
        if any(not o for o in opts):
            issues.append("%s 有空选项" % qid)
        if len(set(opts)) < 4:
            issues.append("%s 选项重复" % qid)
        lens = [_wc(o) for o in opts]
        if min(lens) >= 4 and max(lens) / max(1, min(lens)) > 2.0:
            issues.append("%s 选项长度悬殊 %s" % (qid, lens))
        stem = q.get("stem", "")
        if _wc(stem) > 18:
            issues.append("%s 题干 %d 词（上限 18）" % (qid, _wc(stem)))
        if NEG.search(stem):
            neg += 1

        ex = q.get("explain", "") or ""
        if len(CN.findall(ex)) < 22:
            issues.append("%s 解析须为中文且 ≥22 字（现 %d 个汉字）" % (qid, len(CN.findall(ex))))
        elif len(ex) > 280:
            issues.append("%s 解析过长（%d 字）" % (qid, len(ex)))

        ev = (q.get("evidence") or "").strip()
        if not ev:
            issues.append("%s 缺定位句" % qid)
        elif "…" in ev or "..." in ev:
            issues.append("%s 定位句含省略号，无法高亮" % qid)
        elif ev not in text:
            issues.append("%s 定位句不是原文逐字子串" % qid)

        idx = next((i for i, x in enumerate(qs) if x is q), 0)
        a = ans[idx] if idx < len(ans) else -1
        if 0 <= a < 4:
            cw = re.sub(r"[^\w\s]", "", opts[a]).lower()
            if len(cw.split()) >= 6 and cw in re.sub(r"[^\w\s]", "", text).lower():
                issues.append("%s 正确项整句照抄原文" % qid)

    if neg > 1:
        issues.append("否定问法 %d 题（每节上限 1）" % neg)

    if kind == "reading":
        types = [q.get("type") for q in qs]
        c = Counter(types)
        if any(not t for t in types):
            issues.append("阅读题缺题型标注（main/detail/infer/vocab/attitude）")
        else:
            # 六级五题标准配置：1 主旨 + 细节若干 + 推断 + 词汇/态度；细节题不硬性凑两题
            ok = (c.get("main", 0) == 1 and c.get("detail", 0) >= 1
                  and c.get("infer", 0) >= 1 and c.get("vocab", 0) + c.get("attitude", 0) >= 1)
            if not ok:
                issues.append("阅读题型分布不符：%s" % dict(c))
    return issues


def check_set(data):
    """整套检查，返回 {ok, issues, by_section}。"""
    by_section, all_issues = {}, []
    for sec in data.get("sections", []):
        if not sec.get("questions"):
            continue
        got = check_section(sec)
        if got:
            by_section[sec.get("id") or sec.get("type")] = got
            all_issues += got
    return {"ok": not all_issues, "issues": all_issues, "by_section": by_section}

def rebalance(sec):
    """把答案分布拉匀：同一字母 ≤2 次、至少覆盖 3 种、不出现连续三题相同。

    做法是交换同一题内的选项位置，不改动任何文字——这比让模型重写安全得多。
    """
    qs = [q for q in (sec.get("questions") or []) if len(q.get("options") or []) == 4]
    if len(qs) < 4:
        return 0
    moved = 0

    def counts():
        return Counter(int(q.get("answer", 0)) for q in qs)

    for _ in range(8):
        c = counts()
        if max(c.values()) <= 2 and len(c) >= 3:
            break
        over = max(c, key=lambda k: c[k])
        under = min(range(4), key=lambda k: c[k])
        if c[under] >= 2:
            break
        for q in qs:
            a = int(q.get("answer", 0))
            if a == over:
                opts = q["options"]
                opts[a], opts[under] = opts[under], opts[a]
                q["answer"] = under
                moved += 1
                break

    # 连续三题同一字母
    for i in range(len(qs) - 2):
        seq = [int(q.get("answer", 0)) for q in qs[i:i + 3]]
        if seq[0] == seq[1] == seq[2]:
            q = qs[i + 2]
            cur = int(q.get("answer", 0))
            c = counts()
            for t in range(4):
                if t != cur and c[t] < 2:
                    q["options"][cur], q["options"][t] = q["options"][t], q["options"][cur]
                    q["answer"] = t
                    moved += 1
                    break
    return moved
