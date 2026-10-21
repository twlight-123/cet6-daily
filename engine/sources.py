"""RSS/Atom 源管理与候选文章抓取。"""
import re
import socket
import urllib.request
import xml.etree.ElementTree as ET

from . import extract

# 2026-10 实测可达的源；topic 决定文章归类
FEEDS = [
    # 调查研究
    {"name": "ScienceDaily", "url": "https://www.sciencedaily.com/rss/top/science.xml", "topic": "research"},
    {"name": "Phys.org", "url": "https://phys.org/rss-feed/", "topic": "research"},
    {"name": "Live Science", "url": "https://www.livescience.com/feeds/all", "topic": "research"},
    {"name": "Nature", "url": "https://www.nature.com/nature.rss", "topic": "research"},
    {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index", "topic": "research"},
    {"name": "New Scientist", "url": "https://www.newscientist.com/feed/home/", "topic": "research"},
    {"name": "Quanta", "url": "https://www.quantamagazine.org/feed/", "topic": "research"},
    {"name": "Science News", "url": "https://www.sciencenews.org/feed", "topic": "research"},
    {"name": "MIT News", "url": "https://news.mit.edu/rss/topic/research", "topic": "research"},
    {"name": "Nautilus", "url": "https://nautil.us/feed/", "topic": "research"},
    {"name": "Wired", "url": "https://www.wired.com/feed/rss", "topic": "research"},
    # 时事热点 / 新闻快讯
    {"name": "NPR World", "url": "https://feeds.npr.org/1004/rss.xml", "topic": "news"},
    {"name": "ABC News", "url": "https://abcnews.go.com/abcnews/internationalheadlines", "topic": "news"},
    {"name": "UN News", "url": "https://news.un.org/feed/subscribe/en/news/all/rss.xml", "topic": "news"},
    {"name": "NPR Science", "url": "https://feeds.npr.org/1019/rss.xml", "topic": "news"},
    {"name": "ProPublica", "url": "https://www.propublica.org/feeds/propublica/main", "topic": "news"},
    # 历史故事 / 人文 · 评论文体（六级阅读的主要来源）
    {"name": "Smithsonian History", "url": "https://www.smithsonianmag.com/rss/history/", "topic": "history"},
    {"name": "Smithsonian Arts", "url": "https://www.smithsonianmag.com/rss/arts-culture/", "topic": "history"},
    {"name": "Smithsonian News", "url": "https://www.smithsonianmag.com/rss/smart-news/", "topic": "history"},
    {"name": "Aeon", "url": "https://aeon.co/feed.rss", "topic": "history"},
    {"name": "Psyche", "url": "https://psyche.co/feed.rss", "topic": "history"},
    {"name": "JSTOR Daily", "url": "https://daily.jstor.org/feed/", "topic": "history"},
    {"name": "Longreads", "url": "https://longreads.com/feed/", "topic": "history"},
]

TOPIC_LABEL = {"research": "调查研究", "news": "时事新闻", "history": "历史人文"}

UA = extract.UA


def _strip(fragment: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", fragment or "")
    import html
    txt = html.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


def _children(el, name):
    return [c for c in el.iter() if c.tag.split("}")[-1].lower() == name]


def fetch_feed(feed, timeout=12):
    req = urllib.request.Request(feed["url"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    root = ET.fromstring(data)
    items = []
    for node in root.iter():
        tag = node.tag.split("}")[-1].lower()
        if tag not in ("item", "entry"):
            continue
        title = ""
        link = ""
        desc = ""
        for child in node:
            ct = child.tag.split("}")[-1].lower()
            if ct == "title" and not title:
                title = _strip(child.text or "")
            elif ct in ("link",) and not link:
                link = (child.text or "").strip() or child.attrib.get("href", "")
            elif ct in ("description", "summary", "content") and not desc:
                desc = _strip(child.text or "")
        if title and link:
            items.append({
                "title": title,
                "url": link,
                "summary": desc,
                "source": feed["name"],
                "topic": feed["topic"],
            })
    return items


def gather(limit_per_feed=6, timeout=12):
    """并发抓取所有源，返回候选条目。"""
    import concurrent.futures as cf
    socket.setdefaulttimeout(timeout)
    out, errors = [], []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_feed, f, timeout): f for f in FEEDS}
        for fut in cf.as_completed(futs):
            feed = futs[fut]
            try:
                out.extend(fut.result()[:limit_per_feed])
            except Exception as e:
                errors.append((feed["name"], repr(e)[:70]))
    return out, errors


def build_article(candidate, target=(700, 1400)):
    """下载正文。保留足够长，供后面滑窗取段（真题也是从长文里摘一段）。"""
    try:
        html_text = extract.fetch_html(candidate["url"])
    except Exception:
        return None
    title, paras = extract.extract_article(html_text)
    if not paras:
        return None
    # 去掉与标题重复的引题
    paras = [p for p in paras if p.strip().lower() != (candidate["title"] or "").lower()]
    chosen, n = extract.fit_length(paras, target)
    if n < 380:
        return None
    text = "\n\n".join(chosen)
    return {
        "title": candidate["title"] or title,
        "url": candidate["url"],
        "source": candidate["source"],
        "topic": candidate["topic"],
        "paragraphs": chosen,
        "word_count": n,
        "text": text,
        "passage": extract.slice_best_passage(text),
    }
