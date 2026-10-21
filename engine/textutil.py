"""文本工具：分句、分词、音节估算。"""
import re

_ABBR = {"mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "e.g", "i.e", "u.s", "u.k", "no", "fig", "al"}

def split_sentences(text: str):
    """粗粒度分句，避免在缩写/小数处切断。"""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    out, buf = [], []
    tokens = re.split(r"(?<=[.!?])\s+", text)
    for tk in tokens:
        if not tk.strip():
            continue
        # 若上一句以缩写结尾，合并
        if out and re.search(r"\b[a-z]{1,4}\.$", tk, re.I) is None:
            pass
        tail = tk.rstrip()
        last_word = re.split(r"[\s(]", tail)[-1].rstrip(".").lower()
        if last_word in _ABBR and out is not None:
            buf.append(tk)
            continue
        if buf:
            buf.append(tk)
            out.append(" ".join(buf))
            buf = []
        else:
            out.append(tk)
    if buf:
        out.append(" ".join(buf))
    # 合并过短碎片（如 "Dr." 之类）
    merged = []
    for s in out:
        if merged and len(merged[-1].split()) <= 2:
            merged[-1] = merged[-1] + " " + s
        else:
            merged.append(s)
    return [s.strip() for s in merged if s.strip()]

_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")

def words(text: str):
    return _WORD.findall(text)

def syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 1
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    groups = re.findall(r"[aeiouy]{1,2}", w)
    return max(1, len(groups))

def word_count(text: str) -> int:
    return len(words(text))

def paragraphs(text: str):
    blocks = [p.strip() for p in re.split(r"\n{1,}", text) if p.strip()]
    return blocks or [text.strip()]
