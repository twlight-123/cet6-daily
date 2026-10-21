"""从网页 HTML 中抽取正文段落。零依赖，基于启发式。"""
import html
import re
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

_DROP = re.compile(
    r"<(script|style|nav|footer|header|aside|form|figure|figcaption|iframe|noscript|svg|button|select)\b.*?</\1>",
    re.I | re.S)
_P = re.compile(r"<p\b[^>]*>(.*?)</p>", re.I | re.S)
_H = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\u00a0]+")


def fetch_html(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _clean(fragment: str) -> str:
    txt = _TAG.sub(" ", fragment)
    txt = html.unescape(txt)
    txt = _WS.sub(" ", txt).strip()
    return txt


_BOILER = (
    "browser version", "limited support for css", "javascript", "cookie",
    "we use cookies", "all rights reserved", "sign up for", "newsletter",
    "subscribe to", "follow us", "share this", "advertisement", "read more",
    "click here", "terms of use", "privacy policy", "photo by", "getty images",
    "thank you for visiting", "enable javascript", "your browser",
)


def _looks_like_body(p: str) -> bool:
    if len(p) < 90:
        return False
    if p.count(". ") < 1 and len(p) < 200:
        return False
    letters = sum(c.isalpha() or c.isspace() for c in p)
    if letters / max(1, len(p)) < 0.85:
        return False
    low = p.lower()
    hits = sum(1 for b in _BOILER if b in low)
    if hits and len(p) < 260:
        return False
    if hits >= 2:
        return False
    return True


def extract_article(html_text: str):
    """返回 (title, paragraphs:list[str])"""
    body = _DROP.sub(" ", html_text)
    # 优先 <article>
    m = re.search(r"<article\b.*?</article>", body, re.I | re.S)
    scope = m.group(0) if m else body
    paras = [_clean(p) for p in _P.findall(scope)]
    paras = [p for p in paras if _looks_like_body(p)]
    if len(" ".join(paras).split()) < 180 and m:
        # article 内太短，退回全文
        paras = [_clean(p) for p in _P.findall(body)]
        paras = [p for p in paras if _looks_like_body(p)]
    title = ""
    tm = _H.search(body) or re.search(r"<title\b[^>]*>(.*?)</title>", body, re.I | re.S)
    if tm:
        title = _clean(tm.group(1)).split("|")[0].strip()
    return title, paras


def fit_length(paras, target=(330, 520)):
    """保留完整句子前提下载剪/拼接，命中目标词数区间。"""
    lo, hi = target
    out, n = [], 0
    for p in paras:
        w = len(p.split())
        if n and n + w > hi:
            break
        out.append(p)
        n += w
        if n >= lo:
            break
    return out, n


def slice_best_passage(text, lo=415, hi=455, kind="reading"):
    """在长文里滑窗，挑出最接近六级难度的那一段（以整句为单位，不改写原文）。

    六级真题的阅读从来不是整篇杂志文章，而是从中摘一段再稍作改编。
    整篇去量，长句占比必然超标，于是"整篇不合格"并不等于"这篇不能用"。
    """
    from . import difficulty
    from .textutil import split_sentences

    sents = [s.strip() for s in split_sentences(text) if s.strip()]
    if not sents:
        return None
    n = len(sents)
    best = None
    for i in range(n):
        wc = 0
        for j in range(i, n):
            wc += len(sents[j].split())
            if wc > hi:
                break
            if wc < lo:
                continue
            cand = " ".join(sents[i:j + 1])
            # 句式问题留给 adapt 阶段改；这一步只挑词汇难度最贴近的段落
            m = difficulty.analyze(cand, kind, ignore=("words", "asl", "long_sent"))
            key = (-abs(m["flesch"] - 47.0), wc)
            if best is None or key > best[0]:
                best = (key, cand, m)
    if not best:
        # 全文不足 lo 词时，退而取整篇
        cand = " ".join(sents)
        return {"text": cand, "measure": difficulty.analyze(cand, kind, ignore=("words",))}
    return {"text": best[1], "measure": best[2]}
