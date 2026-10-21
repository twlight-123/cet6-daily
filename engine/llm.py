"""可选的大模型通道：任何 OpenAI 兼容接口（DeepSeek / Kimi / 智谱 / 通义 / 本地 Ollama）。"""
import json
import re
import urllib.parse
import urllib.request

SYSTEM = (
    "你是中国大学英语六级考试（CET-6）命题组的资深命题人，同时精通语篇分析与测试学。"
    "你的任务是把真实外刊文章改编成六级真题水准的练习套题。"
    "命题必须遵守：干扰项用同义改写而非原文复现；错误项须有明确的致错机制"
    "（张冠李戴、绝对化、因果倒置、过度推断、偷换概念）；不使用超纲生僻词作考点。"
    "只输出 JSON，不要输出解释性文字或 Markdown 代码块。"
)

SCHEMA_HINT = """输出严格符合以下结构的 JSON：
{
 "listening": [
   {"kind":"long_conversation","title":"Conversation 1","scene":"场景一句话",
    "script":[{"sp":"W","en":"..."},{"sp":"M","en":"..."}],
    "questions":[{"type":"main|detail|infer|attitude","stem":"...",
                  "options":["...","...","...","..."],"answer":0,
                  "explain":"中文解析","evidence":"原文定位句"}]},
   {"kind":"passage","title":"Passage","script":[{"sp":"N","en":"..."}],
    "questions":[{"type":"main|detail|infer", ...}]}
 ],
 "reading": {"title":"...","passage":"分段用 \\n\\n 分隔",
             "questions":[{"type":"main|detail|infer|vocab|attitude", ...}]},
 "cloze": {"intro":"一句话导语","answers":[{"index":1,"pos":"n","gloss":"中文释义"}],
           "notes":["语法/搭配考点说明", ...]},
 "vocab": [{"word":"...","pos":"n","gloss":"中文释义","sentence":"原文例句"}]
}
"""

# 命题铁律：六级真题的客观特征，写死在提示里，另由 engine/itemcheck.py 复核
RULES = """【命题铁律（六级规格，逐条执行）】
1. 每题 4 个选项，同一题内结构一致（同为名词短语 / 从句 / 不定式 / 形容词），词数接近，最长与最短相差不超过 40%。
2. 正确项必须同义改写，不得照抄原文连续 6 词以上；每个干扰项都要有明确致错机制（张冠李戴、绝对化、因果倒置、过度推断、偷换概念、无关信息）。
3. 答案分布：5 题中至少覆盖 3 个不同字母，同一字母作答案不超过 2 次，不得连续三题同一字母。
4. 题干 ≤ 15 词；每节最多 1 题用否定问法（NOT / EXCEPT）。
5. explain 必须是中文，60–120 字：先给正确依据，再逐个点明三个干扰项错在哪（"A 项…；B 项…；C 项…"）。
6. evidence 必须是原文（听力为原台词）中的连续句子，逐字一致，不得改写、不得加省略号。
7. 题型必须标注：reading 恰好 1 题 main、2 题 detail、1 题 infer、1 题 vocab 或 attitude；长对话覆盖 main/detail/infer/attitude；篇章覆盖 main/detail/infer。"""



def _post(url, payload, api_key, timeout=120):
    req = urllib.request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % api_key,
        },
        method="POST",
    )
    # 本地地址（Ollama / 自建代理）必须绕过系统代理，否则会被 502
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


# 各家接口只认 model id；界面上的显示名（如 "DeepSeek V4.1 Flash"）填进来会 400
_MODEL_DEFAULT = [
    ("deepseek", "deepseek-chat"),
    ("moonshot", "moonshot-v1-8k"),
    ("dashscope", "qwen-plus"),
    ("bigmodel", "glm-4-flash"),
    ("open.bigmodel", "glm-4-flash"),
    ("volces", "doubao-pro-32k"),
    ("openai", "gpt-4o-mini"),
    ("localhost", "llama3.1"),
    ("127.0.0.1", "llama3.1"),
]


def resolve_model(settings):
    """把显示名/空值换成该服务商可用的 model id。"""
    llm = settings.get("llm", {})
    model = (llm.get("model") or "").strip()
    bad = (not model) or (" " in model) or (model.lower() == "default")
    if bad:
        base = (llm.get("base_url") or "").lower()
        for key, default in _MODEL_DEFAULT:
            if key in base:
                return default
        return "deepseek-chat"
    return model


def is_configured(settings):
    llm = settings.get("llm", {})
    return bool(llm.get("enabled") and llm.get("api_key") and llm.get("base_url"))


def generate(settings, article, topics_label):
    """返回 dict（listening/reading/cloze/vocab）或 None。"""
    llm = settings["llm"]
    prompt = f"""下面是今天选取的外刊文章（来源 {article['source']}，主题归类：{topics_label}）。
请据此出一套六级练习。

【原文标题】{article['title']}
【原文正文】
{article['text']}

【任务要求】
1. listening：
   - 一篇 long_conversation（一男一女两位说话人，320–370 词，场景贴近校园/职场/社会话题，与原文主题同一领域但不复述）；
   - 一篇 passage（独白式短文，300–350 词，说明或新闻述评性质，首尾成篇）。
   - 各出 4 道和 3 道单选题，覆盖：主旨、细节、推断、说话人态度/目的。选项 4 个，只有一个正确。
2. reading：以原文为基础改编成长度 420–455 词的六级阅读篇章，保留学术/新闻语体；段落要完整，4–6 段，每段一个论点，段与段之间用空行分隔，开头点题、结尾收束，不要写成摘要；
   出 5 道单选题，题型分布为 1 主旨题 + 2 细节题 + 1 推断题 + 1 词义/态度题。
3. cloze：从 reading.passage 中选 10 个 CET-6 量级实词（避免专有名词、避免首句），
   在其出现位置用 [1]…[10] 标记并改写 reading.passage（标记后仍要能读通）。
   在 cloze.answers 中依次给出 index / pos / gloss（中文释义）。
4. 每题必须给出 explain（不超过 120 字，含正确原因与主要干扰项为何错）和 evidence（原文定位句）。
5. 全部英文文本难度对标六级：平均句长 18–24 词，含适量从句与抽象名词，避免口语化到四级以下。
6. vocab：从文中挑 6 个六级核心词，给词性、中文释义、原文例句。

{RULES}

{SCHEMA_HINT}"""
    payload = {
        "model": resolve_model(settings),
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.75,
        "max_tokens": 8000,
    }
    try:
        resp = _post(llm["base_url"], payload, llm["api_key"], llm.get("timeout", 120))
        content = resp["choices"][0]["message"]["content"]
    except Exception as e:
        raise RuntimeError("大模型调用失败：%s" % repr(e)[:160])
    data = _extract_json(content)
    if not isinstance(data, dict):
        raise RuntimeError("大模型返回内容不是合法 JSON")
    return data

READING_PROMPT = """下面是今天选取的外刊文章（来源 {source}）。

【标题】{title}
【正文】
{text}

【任务】把它改写成一篇适合中国大学生课外阅读的中英对照短文：
1. 英文正文 4–6 段，每段 80–120 词，总词数 320–520；内容尽量完整，保留原文的事实、论证与关键细节，不要压成摘要。
2. 每段给出准确、自然的中文翻译（不要机翻腔，不要逐字硬译）。
3. title_en 用英文标题（不超过 9 个词），title_zh 用中文标题。
4. vocab：挑 4 个六级核心词，给 word 与中文 gloss。

严格输出 JSON：
{{"title_en":"...","title_zh":"...","paragraphs":[{{"en":"...","zh":"..."}}],"vocab":[{{"word":"...","gloss":"..."}}]}}"""


def generate_reading(settings, article):
    """生成当日中英对照阅读。返回 dict 或 None。"""
    llm = settings.get("llm", {})
    prompt = READING_PROMPT.format(source=article.get("source", ""),
                                   title=article.get("title", ""),
                                   text=article.get("text", "")[:6000])
    payload = {
        "model": resolve_model(settings),
        "messages": [
            {"role": "system", "content": "你是英语读物编辑，中文表达要地道。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 3000,
    }
    resp = _post(llm["base_url"], payload, llm["api_key"], llm.get("timeout", 120))
    data = _extract_json(resp["choices"][0]["message"]["content"])
    if not isinstance(data, dict) or not data.get("paragraphs"):
        return None
    return data

# ---------------- 译文润色 ----------------
TRANSLATE_SYSTEM = (
    "你是中文出版物的资深译者，长期翻译英文科普与新闻报道。"
    "要求：准确传达原意，中文自然流畅，符合书面语习惯；不要逐字硬译，不要添加原文没有的信息；"
    "人名、机构名、术语按通用译法处理，首次出现可保留英文原名。"
)

TRANSLATE_PROMPT = """把下面这篇英文短文的每一段翻译成中文。

【标题】{title}
【段落】
{blocks}

要求：
1. 逐段对应翻译，段落数必须与原文一致（共 {n} 段）；
2. 中文要像发表在中文媒体上的文章，而不是机翻；
3. 保留专有名词的通用译名，必要时在括号里附英文；
4. 只输出 JSON：{{"zh": ["第一段译文", "第二段译文", ...]}}"""


def chat(settings, messages, temperature=0.4, max_tokens=4000, timeout=None):
    """通用对话调用，返回正文字符串。"""
    llm = settings.get("llm", {})
    payload = {
        "model": resolve_model(settings),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    resp = _post(llm["base_url"], payload, llm["api_key"],
                 timeout or llm.get("timeout", 120))
    return resp["choices"][0]["message"]["content"]


def ping(settings, timeout=30):
    """测试连接：返回 (ok, 信息)。"""
    try:
        out = chat(settings, [{"role": "user", "content": "只回复两个字：可用"}],
                   temperature=0, max_tokens=16, timeout=timeout)
        return True, (out or "").strip()[:40]
    except Exception as e:
        return False, repr(e)[:160]


def translate_blocks(settings, title, blocks):
    """把若干段英文译成高质量中文，返回与 blocks 等长的译文列表。"""
    if not blocks:
        return []
    numbered = "\n".join("[%d] %s" % (i + 1, b) for i, b in enumerate(blocks))
    prompt = TRANSLATE_PROMPT.format(title=title or "", blocks=numbered, n=len(blocks))
    content = chat(settings, [
        {"role": "system", "content": TRANSLATE_SYSTEM},
        {"role": "user", "content": prompt},
    ], temperature=0.35, max_tokens=3000)
    data = _extract_json(content)
    zh = (data or {}).get("zh") if isinstance(data, dict) else None
    if not isinstance(zh, list) or len(zh) != len(blocks):
        raise RuntimeError("译文结构不符")
    return [str(x).strip() for x in zh]

PARA_SYSTEM = ("你是英文读物的编辑。你的任务只是给一段连续英文分段，"
               "不改动任何字词。")

PARA_PROMPT = """下面是一段英文文章（可能整篇挤在一段里）。

请在**语义转折处**分段：
1. 一个自然段只讲一个完整的意思或一个论证步骤；
2. 不要为了段落数量均匀而切断本该连在一起的句子；
3. 原文一个字都不要改，只插入空行；
4. 通常 3–6 段，宁可少分也不要硬凑。

只输出分段后的正文。

%s"""


def reparagraph(settings, text):
    """让模型按语义分段，返回新文本；校验失败返回 None。"""
    if not settings or not text or not is_configured(settings):
        return None
    try:
        out = chat(settings, [
            {"role": "system", "content": PARA_SYSTEM},
            {"role": "user", "content": PARA_PROMPT % text},
        ], temperature=0.2, max_tokens=2600)
    except Exception:
        return None
    out = (out or "").strip()
    if out.startswith("```"):
        out = re.sub(r"^```[a-z]*\n|```$", "", out).strip()
    # 严格校验：去掉换行后必须与原文逐字一致，防止模型顺手改写
    def flat(x):
        return re.sub(r"\s+", " ", x).strip()
    if flat(out) != flat(text):
        return None
    paras = [p for p in out.split("\n\n") if p.strip()]
    if not 2 <= len(paras) <= 8:
        return None
    return "\n\n".join(paras)

def fix_questions(settings, section, issues):
    """按质检意见重做某一节的题目，返回新 questions 列表或 None。"""
    kind = section.get("type")
    body = section.get("passage") or "\n".join(
        l.get("en", "") for l in (section.get("script") or []))
    cur = json.dumps(section.get("questions") or [], ensure_ascii=False, indent=1)
    prompt = (
        "下面是一节六级练习的原文与现有题目，质检不合格，请重做这一节的全部题目。\n\n"
        "【题型】%s\n\n【原文】\n%s\n\n【现有题目】\n%s\n\n【不合格原因】\n%s\n\n"
        "【任务】逐条修正上述问题，并严格遵守下面的命题铁律。\n\n%s\n\n"
        '只输出 JSON：{"questions":[{"type":"...","stem":"...",'
        '"options":["...","...","...","..."],"answer":0,'
        '"explain":"中文解析","evidence":"原文定位句"}]}'
    ) % (kind, body[:6000], cur, "\n".join("- " + x for x in issues), RULES)
    try:
        out = chat(settings, [{"role": "system", "content": SYSTEM},
                              {"role": "user", "content": prompt}],
                   temperature=0.45, max_tokens=3200)
        data = _extract_json(out)
        qs = (data or {}).get("questions")
        return qs if isinstance(qs, list) and len(qs) >= 3 else None
    except Exception:
        return None
