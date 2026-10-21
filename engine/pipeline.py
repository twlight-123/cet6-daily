"""每日出题流水线：抓文章 → 分级筛选 → 生成套题 → 落盘。"""
import datetime
import random
import re
import threading
import time
import traceback

from . import cloze, difficulty, extract, grade, llm, sources, store, vocab
from .textutil import split_sentences

STATUS = {"state": "idle", "message": "", "finished_at": None, "log": []}
_lock = threading.Lock()


def _log(msg):
    line = "%s %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg)
    STATUS["log"].append(line)
    STATUS["log"] = STATUS["log"][-40:]
    STATUS["message"] = msg


def _pick_articles(count=2, per_feed=10):
    cands, errors = sources.gather(limit_per_feed=per_feed)
    for name, err in errors:
        _log("源不可用 %s: %s" % (name, err))
    if not cands:
        raise RuntimeError("所有新闻源都不可达，请检查网络或代理")

    random.shuffle(cands)
    seen_titles = set()
    by_topic = {}
    for c in cands:
        key = c["title"].lower()[:60]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        by_topic.setdefault(c["topic"], []).append(c)

    # 主题轮转，保证题材多样
    order = ["research", "news", "history"]
    pool = []          # 合格的
    spare = []         # 不合格但可兜底的
    idx = {t: 0 for t in order}
    guard = 0
    need = max(8, count * 8)        # 多扫一些：单篇不合规是常态，别扫两眼就下结论
    while len(pool) + len(spare) < need and guard < 40:
        guard += 1
        progressed = False
        for t in order:
            arr = by_topic.get(t) or []
            while idx[t] < len(arr):
                c = arr[idx[t]]
                idx[t] += 1
                if any(x["url"] == c["url"] for x in pool + spare):
                    continue
                art = sources.build_article(c)
                if not art:
                    _log("跳过（正文抽取不足）: %s" % c["title"][:45])
                    continue
                m = grade.measure(art["text"])
                art["measure"] = m
                flag = "命中" if m["in_band"] else "偏离 %.0f" % m["distance"]
                _log("候选 %-14s %5d词 FRE%5.1f 句长%4.1f %-8s %s" % (
                    c["source"], m["words"], m["flesch_ease"], m["avg_sentence_len"],
                    flag, c["title"][:34]))
                (pool if m["in_band"] else spare).append(art)
                progressed = True
                break
            if len(pool) + len(spare) >= need:
                break
        if not progressed:
            break

    _log("扫描 %d 篇：命中六级区间 %d 篇，其余按接近程度排序" % (len(pool) + len(spare), len(pool)))
    picked = pool[:count]
    if not picked:
        # 今天没有完全命中的稿子：取最接近的，别直接罢工
        spare.sort(key=lambda a: a["measure"]["distance"])
        picked = spare[:count]
        if picked:
            _log("今日无命中区间的文章，改用最接近的 %d 篇（FRE %.1f–%.1f）" % (
                len(picked), picked[0]["measure"]["flesch_ease"],
                picked[-1]["measure"]["flesch_ease"]))
    if not picked:
        raise RuntimeError("今天没抓到难度合适的文章，稍后再试或换源")
    return picked


def _long_sentences(text, n=3):
    sents = [s for s in split_sentences(text) if 25 <= len(s.split()) <= 70]
    sents.sort(key=lambda s: -len(s.split()))
    return [{"en": s, "words": len(s.split())} for s in sents[:n]]


def _make_cloze(article, seed):
    # 选词填空正文按真题规格取 270–310 词，不是整篇照搬
    from . import extract
    sl = extract.slice_best_passage(article["text"], lo=270, hi=310, kind="banked_cloze")
    text = sl["text"] if sl else article["text"]
    res = cloze.build(text, seed=seed)
    if not res:
        return None
    # 补充每个空的上下文句，便于解析
    sents = split_sentences(text)
    for a in res["answers"]:
        marker = "[%d]" % a["index"]
        for s in sents:
            if marker not in s:
                continue
            ctx = s.replace(marker, a["word"])
            a["context"] = ctx
            break
    return res



def _strip_blanks(text):
    """把 [1]…[10] 挖空标记去掉，还原成干净的阅读正文。"""
    t = re.sub(r"\s*\[\d+\]\s*", " ", text or "")
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def _fix_evidence(q, text):
    """大模型给的定位句常带省略号或与正文有细微差异，改成正文里的逐字子串。"""
    ev = (q.get("evidence") or "").strip()
    flat = re.sub(r"\s+", " ", text or "")
    if not ev or ev in flat:
        return ev
    for part in sorted([x.strip() for x in re.split(r"…+|\.\.\.", ev) if len(x.strip()) > 12],
                       key=len, reverse=True):
        if part in flat:
            return part
    sents = re.split(r"(?<=[.!?])\s+", flat)
    keys = [w for w in re.findall(r"[A-Za-z]{5,}", ev)][:5]
    best, hit = "", 0
    for st in sents:
        n = sum(1 for w in keys if w.lower() in st.lower())
        if n > hit:
            best, hit = st, n
    return best.strip() if hit >= 2 else ev


_MARKERS = ("However", "But ", "Yet ", "Still,", "Critics", "Supporters", "For ", "In ",
            "That ", "This ", "These ", "Meanwhile", "By contrast", "As a result",
            "Another", "Others", "Some ", "Many ", "The result", "At the same time",
            "Yet the", "Even so", "Of course", "What ", "Whether ")


def _ensure_paragraphs(text, want=5, settings=None):
    """按语义分段：先让模型判断断点（不改字词），失败就只按转折词切，绝不均分。

    按句子数硬切会切断论证，读起来是「乱分段」——这类判断只能看意思，不能看数量。
    """
    text = (text or "").strip()
    if not text:
        return text
    if len([p for p in text.split("\n\n") if p.strip()]) >= 3:
        return text                      # 模型已经分好段，不再动

    from . import llm as _llm
    if settings and _llm.is_configured(settings):
        fixed = _llm.reparagraph(settings, text)
        if fixed:
            return fixed

    # 兜底：只在明确的转折词处断，且每段至少两句；找不到就不分
    sents = split_sentences(text)
    if len(sents) < 6:
        return text
    strong = ("However", "But ", "Yet ", "Still,", "Critics", "Supporters", "By contrast",
              "In contrast", "Meanwhile", "As a result", "For decades", "For years",
              "At the same time", "Even so", "On the other hand")
    cuts = [i for i, st in enumerate(sents) if i >= 2 and st.lstrip().startswith(strong)]
    if len(cuts) < 2:
        return text
    groups, prev = [], 0
    for c in cuts[:6]:
        groups.append(" ".join(sents[prev:c]))
        prev = c
    groups.append(" ".join(sents[prev:]))
    groups = [g for g in groups if g.strip()]
    return "\n\n".join(groups) if len(groups) >= 2 else text


def _assemble_llm(gen, articles, seed, settings=None):
    sections = []
    listening = gen.get("listening") or []
    for i, item in enumerate(listening):
        qs = []
        for j, q in enumerate(item.get("questions") or [], 1):
            qs.append({
                "id": "l%d_%d" % (i + 1, j),
                "stem": q.get("stem", ""),
                "options": q.get("options", [])[:4],
                "answer": int(q.get("answer", 0)),
                "explain": q.get("explain", ""),
                "evidence": q.get("evidence", ""),
            })
        if not qs:
            continue
        stype = "long_conversation" if item.get("kind") == "long_conversation" else "passage_listening"
        sections.append({
            "id": "sec_l%d" % (i + 1),
            "type": stype,
            "title": item.get("title") or ("Conversation %d" % (i + 1) if stype == "long_conversation" else "Passage %d" % (i + 1)),
            "scene": item.get("scene", ""),
            "script": item.get("script") or [],
            "questions": qs,
        })

    reading = gen.get("reading") or {}
    r_passage = _ensure_paragraphs(_strip_blanks(reading.get("passage") or ""), settings=settings)
    rqs = []
    for j, q in enumerate(reading.get("questions") or [], 1):
        rqs.append({
            "id": "r_%d" % j,
            "stem": q.get("stem", ""),
            "options": q.get("options", [])[:4],
            "answer": int(q.get("answer", 0)),
            "explain": q.get("explain", ""),
            "evidence": q.get("evidence", ""),
        })
    for q in rqs:
        q["evidence"] = _fix_evidence(q, r_passage)
    if r_passage and rqs:
        sections.append({
            "id": "sec_r",
            "type": "reading",
            "title": reading.get("title") or "仔细阅读",
            "passage": r_passage,
            "source": {"name": articles[0]["source"], "url": articles[0]["url"]},
            "questions": rqs,
        })

    # 选词填空：从阅读正文里另取 265–315 词的一段单独挖空。
    # 不能直接用阅读正文——四五百词的篇章塞进 15 选 10 不合规格，且会把空格标进阅读题。
    base = articles[0]["text"]
    try:
        sl = extract.slice_best_passage(r_passage, lo=282, hi=306, kind="banked_cloze")
        if sl:
            base = sl["text"]
    except Exception:
        pass
    cz = cloze.build(base, seed=seed)
    if cz:
        cz["passage"] = _ensure_paragraphs(cz["passage"], want=3, settings=settings)
        for a in cz["answers"]:
            a["gloss"] = vocab.gloss_of(a.get("lower") or a["word"]) or ""
            a["context"] = ""
            for sent in split_sentences(cz["passage"].replace("[%d]" % a["index"], a["word"])):
                if a["word"] in sent:
                    a["context"] = sent.strip()
                    break
        sections.append({
            "id": "sec_c",
            "type": "banked_cloze",
            "title": "选词填空",
            "instruction": "从下面 15 个选项中为文中 10 个空格选出最佳答案，每个选项只能用一次。",
            "passage": cz["passage"],
            "bank": cz["bank"],
            "answers": cz["answers"],
        })
    return sections


def _assemble_local(articles, seed, settings=None):
    """本地引擎：阅读板块用「摘段 + 编辑」产出，选词填空从同一段挖空。"""
    art = articles[0]
    sections = []

    passage = art.get("passage")
    if passage:
        from . import editor
        text, m, edits = editor.edit(passage["text"])
        _log("阅读摘段：%d 词，FRE %.1f，句长 %.1f，编辑动作 %s" % (
            len(text.split()), m["flesch"], m["asl"], "+".join(dict.fromkeys(edits)) or "无"))
        # 本地引擎不写题目，把成品段落留给命题流程直接用，避免出现空题板块
        art["passage_text"] = text
        art["passage_measure"] = m

    cz = _make_cloze(art, seed)
    if cz:
        cz["passage"] = _ensure_paragraphs(cz["passage"], want=3, settings=settings)
        sections.append({
            "id": "sec_c",
            "type": "banked_cloze",
            "title": "选词填空",
            "instruction": "从下面 15 个选项中为文中 10 个空格选出最佳答案，每个选项只能用一次。",
            "passage": cz["passage"],
            "bank": cz["bank"],
            "answers": cz["answers"],
        })
    return sections


def build_today(force=False, set_id=None):
    """主入口。返回 (ok, payload)。"""
    with _lock:
        STATUS.update({"state": "running", "message": "开始抓取文章", "log": [], "finished_at": None})
    today = set_id or datetime.date.today().isoformat()
    try:
        st = store.settings()
        store.ensure_dirs()
        if not force and store.load_set(today):
            _log("%s 已有内容，跳过" % today)
            STATUS.update({"state": "done", "finished_at": time.time()})
            return True, {"skipped": True, "id": today}

        _log("抓取 RSS 候选…")
        articles = _pick_articles(count=2)
        seed = hash(today) & 0xFFFF

        engine = "local"
        gen = None
        sections = None
        from . import repair
        if llm.is_configured(st):
            topic_label = "、".join(sources.TOPIC_LABEL.get(a["topic"], a["topic"]) for a in articles)
            best = None
            # 一次出稿很难刚好达标：校订后仍不进带就重出，最多三轮，取最高分那套
            for attempt in range(1, 4):
                _log("第 %d 次出题：调用大模型…" % attempt)
                try:
                    g = llm.generate(st, articles[0], topic_label)
                except Exception as e:
                    _log("大模型调用失败：%s" % repr(e)[:100])
                    break
                secs = _assemble_llm(g, articles, seed, settings=st)
                if not secs:
                    continue
                try:
                    from . import itemcheck as _ic
                    moved = sum(_ic.rebalance(s) for s in secs)
                    if moved:
                        _log("答案分布校正：移动 %d 题的正确项位置" % moved)
                except Exception:
                    pass
                try:
                    for line in repair.apply(secs, st):
                        _log("自动校订：" + line)
                except Exception as e:
                    _log("自动校订出错：%s" % repr(e)[:80])
                chk = difficulty.check_set({"sections": secs})
                _log("第 %d 次结果：%d 分，%s" % (attempt, chk["score"],
                                               "达标" if chk["in_band"] else "未达标"))
                if best is None or chk["score"] > best[0]:
                    best = (chk["score"], secs, g, chk["in_band"])
                if chk["in_band"]:
                    break
            if best:
                sections, gen, engine = best[1], best[2], "llm"
                if not best[3]:
                    _log("三轮均未完全达标，取最高分 %d 分的那套" % best[0])

        if not sections:
            sections = _assemble_local(articles, seed, settings=st)
            try:
                for line in repair.apply(sections, st):
                    _log("自动校订：" + line)
            except Exception as e:
                _log("自动校订出错：%s" % repr(e)[:80])
        if not sections:
            raise RuntimeError("文章不足以生成题目（可挖空词太少）")

        art = articles[0]
        payload = {
            "id": today,
            "date": today,
            "title": art["title"],
            "source": "auto",
            "engine": engine,
            "topics": sorted({sources.TOPIC_LABEL.get(a["topic"], a["topic"]) for a in articles}),
            "minutes": 35 if engine == "llm" else 15,
            "measure": art.get("measure"),
            "sections": sections,
            "reading_ref": {
                "source": art["source"], "url": art["url"],
                "long_sentences": _long_sentences(art["text"]),
            },
            "reading_draft": ({
                "text": art["passage_text"],
                "words": len(art["passage_text"].split()),
                "measure": art.get("passage_measure"),
                "url": art["url"], "source": art["source"], "title": art["title"],
            } if art.get("passage_text") else None),
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        if engine == "local":
            payload["notice"] = ("今日为本地算法引擎生成：仅「选词填空」可自动出题。"
                                 "配置大模型接口（设置页）后，可自动生成听力与阅读全套题目。")

        # 难度验收：校订与重试后仍不达标的，把偏差写在明面上
        chk = difficulty.check_set(payload)
        payload["difficulty"] = {
            "score": chk["score"], "level": chk["level"], "in_band": chk["in_band"],
            "sections": [{k: s[k] for k in ("type", "words", "asl", "flesch", "fkgl",
                                            "long_sent", "beyond", "in_band", "score")}
                         for s in chk["sections"]],
        }
        if not chk["in_band"]:
            off = [i for s in chk["sections"] for i in s["issues"]]
            _log("难度未达标（%d 分）：%s" % (chk["score"], "；".join(off[:3])))
            payload["notice"] = (payload.get("notice") or "") + \
                " 注意：今日自动题难度评分 %d（%s），与六级区间有偏差。" % (chk["score"], chk["level"])

        # 命题质检：题干 / 选项 / 答案分布 / 中文解析 / 定位句逐项过一遍，不合规退回重做
        try:
            from . import itemcheck
            for rnd in range(2):
                rep = itemcheck.check_set({"sections": sections})
                if rep["ok"]:
                    break
                _log("命题质检 %d 项不合格，退回重做（第 %d 轮）：%s" % (
                    len(rep["issues"]), rnd + 1, rep["issues"][0][:50]))
                for sec in sections:
                    got = rep["by_section"].get(sec.get("id") or sec.get("type"))
                    if not got:
                        continue
                    fixed = llm.fix_questions(st, sec, got)
                    if not fixed:
                        continue
                    first = (sec.get("questions") or [{}])[0].get("id", "")
                    prefix = re.sub(r"\d+$", "", first) or (sec.get("id", "q") + "_")
                    for j, q in enumerate(fixed, 1):
                        q["id"] = "%s%d" % (prefix, j)
                    sec["questions"] = fixed
            left = itemcheck.check_set({"sections": sections})
            if left["ok"]:
                _log("命题质检通过")
            else:
                _log("命题质检仍有 %d 项未过：%s" % (len(left["issues"]), "；".join(left["issues"][:2])))
        except Exception as e:
            _log("命题质检出错：%s" % repr(e)[:80])

        # 每日一文：直接用当天原文（最完整）+ 联网/模型翻译；失败才退回模型改写或内置文库
        rd_ok = False
        try:
            from . import daily_reading
            rd_item = daily_reading.build_from_article(art, today)
            if rd_item:
                rd_ok = True
                _log("每日一文：%s（%d 段 %d 词，已翻译）" % (
                    rd_item["title_en"][:40], len(rd_item["paragraphs"]),
                    sum(len(p["en"].split()) for p in rd_item["paragraphs"])))
        except Exception as e:
            _log("每日一文生成失败，改用内置文库：%s" % repr(e)[:80])

        # 原文抓取失败时，才让模型按主题改写一篇
        try:
            if not rd_ok and gen is not None and llm.is_configured(st):
                rd = llm.generate_reading(st, art)
                if rd:
                    from . import reading as _reading
                    _reading.save({
                        "id": "daily-%s" % today,
                        "kind": "news",
                        "source": "daily",
                        "date": today,
                        "source_label": "时事热点 · %s" % art.get("source", ""),
                        "title_en": rd.get("title_en") or art["title"][:60],
                        "title_zh": rd.get("title_zh") or "",
                        "minutes": max(3, round(sum(len(p.get("en", "").split())
                                                    for p in rd["paragraphs"]) / 120)),
                        "paragraphs": [{"en": p.get("en", ""), "zh": p.get("zh", "")}
                                       for p in rd["paragraphs"] if p.get("en")],
                        "vocab": rd.get("vocab") or [],
                    })
                    _log("课外阅读：已用大模型生成中英对照")
        except Exception as e:
            _log("课外阅读生成失败（不影响出题）：%s" % repr(e)[:80])

        store.save_json(store.set_path(today), payload)
        _log("完成：%s（引擎 %s，%d 个板块）" % (today, engine, len(sections)))
        STATUS.update({"state": "done", "finished_at": time.time()})
        return True, {"ok": True, "id": today, "engine": engine, "sections": len(sections)}
    except Exception as e:
        tb = traceback.format_exc(limit=3)
        _log("失败：%s" % e)
        STATUS.update({"state": "error", "message": str(e), "finished_at": time.time()})
        return False, {"error": str(e), "trace": tb}


def run_async(force=False):
    def _run():
        build_today(force=force)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t
