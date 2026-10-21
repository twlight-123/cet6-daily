"""CET-6 词库：词性、难度、易词表。"""
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
LEX_PATH = os.path.join(_ROOT, "data", "lexicon.txt")

_BY_POS = {"v": set(), "n": set(), "adj": set(), "adv": set()}
_WORD2POS = {}

def _load():
    if _WORD2POS:
        return
    try:
        with open(LEX_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" not in line:
                    continue
                w, pos = line.split("|", 1)
                w, pos = w.strip().lower(), pos.strip()
                if pos not in _BY_POS:
                    continue
                _BY_POS[pos].add(w)
                _WORD2POS[w] = pos
    except FileNotFoundError:
        pass

_load()

# 基础易词：出题时不应被挖空
EASY = set("""
about above accept across act action add admit adult after again against age agree ahead
aim air allow almost alone along already also although always among amount another answer
any anyone anything appear apply area argue arm around arrive art article ask attend author
available avoid away baby back bad bag ball bank base basic be beat beautiful because become
bed before begin behind believe benefit best better between beyond big bill bit black blood
blue board body book born both box boy break bring brother build business buy call camera
campaign can capital car card care career carry case catch cause cell center century certain
chair challenge chance change check child choice choose church citizen city civil claim class
clean clear close coach cold college color come comment common company compare computer
concern condition conference consider contain continue control cook cool copy corner cost
could country couple course court cover create crime cross crowd culture cup current customer
cut dark data daughter day dead deal death debate decade decide decision deep defense degree
demand describe design despite detail determine develop die difference difficult dinner
direction discover discuss disease doctor door doubt down draw dream drink drive drop drug
during each early east easy eat economic edge education effect effort eight either election
else employee end energy enjoy enough enter entire environment especially establish even
event ever every everyone everything evidence exactly example executive exist expect
experience explain eye face fact factor fail fall family far fast father fear federal feel
feeling few field fight figure fill film final finally find fine finger finish fire firm
first fish five floor fly focus follow food foot force foreign forget form former forward
four free friend from front full fund future game garden general get girl give glass go goal
good government great green ground group grow growth guess gun guy hair half hand hang happen
happy hard have head health hear heart heat heavy help her here herself high him himself his
history hit hold home hope hospital hot hotel hour house how however huge human hundred
husband idea identify if image imagine impact important improve include increase indeed
indicate individual industry information inside instead institution interest international
interview into investment involve issue item its itself job join just keep key kid kill kind
kitchen know knowledge land language large last late later laugh law lawyer lay lead leader
learn least leave left leg less let letter level lie life light like likely line list listen
little live local long look lose loss lot love low machine magazine main maintain major make
man manage manager many market material matter may maybe me mean measure media medical meet
member memory mention message method middle might military million mind minute miss mission
model modern moment money month more morning most mother mouth move movement movie much music
must my myself name nation national natural nature near nearly necessary need network never
new news newspaper next nice night no none nor north not note nothing notice now number
occur off offer office officer official often oh oil ok old on once one only onto open
operation opportunity option or order organization other others our out outside over own
owner page pain paper parent part participant particular particularly partner party pass past
patient pattern pay peace people per perform perhaps period person personal phone physical
pick picture piece place plan plant play player point police policy political poor popular
population position possible power practice prepare present president pressure pretty prevent
price private probably problem process produce product professional program project property
protect prove provide public pull purpose push put quality question quickly quite race radio
raise range rate rather reach read ready real reality realize really reason receive recent
recently recognize record red reduce reflect region relate relationship religious remain
remember remove report represent require research resource respond response responsibility
rest result return reveal rich right rise risk road rock role room rule run safe same save say
scene school science score sea season seat second section security see seek seem sell send
senior sense series serious serve service set seven several shake share she shoot short
should shoulder show side sign significant similar simple simply since sing single sister sit
site situation six size skill skin small smile so social society soldier some somebody
someone something sometimes son song soon sort sound source south space speak special
specific speech spend sport spring staff stage stand standard star start state statement
station stay step still stock stop store story strategy street strong structure student
study stuff style subject success successful such suddenly suffer suggest summer support
sure surface system table take talk task tax teach teacher team technology television tell
ten tend term test than thank that the their them themselves then theory there these they
thing think third this those though thought thousand threat three through throughout throw
thus time to today together tonight too top total tough toward town trade traditional
training travel treat treatment tree trial trip trouble true truth try turn two type under
understand unit until up upon us use usually value various very victim view violence visit
voice vote wait walk wall want war watch water way we weapon wear week weight well west what
whatever when where whether which while white who whole whom whose why wide wife will win
wind window wish with within without woman wonder word work worker world worry would write
writer wrong yard year yes yet you young your yourself
""".split())

_SUFFIX_POS = [
    (("ally", "ously", "ively", "ably", "ibly", "ently", "antly", "ingly", "edly"), "adv"),
    (("tion", "sion", "ment", "ness", "ity", "ance", "ence", "ship", "ism", "hood", "acy",
      "ology", "graphy", "ian", "ist", "or", "er", "ure", "age", "ery", "dom"), "n"),
    (("ize", "ise", "ify", "ate", "ise"), "v"),
    (("ous", "ive", "able", "ible", "ful", "less", "ical", "ic", "ary", "ant", "ent",
      "al", "ial", "ian", "ese", "ish", "like", "proof", "worthy"), "adj"),
]

_INFLECT = [("ies", "y"), ("ied", "y"), ("ying", "y"), ("es", ""), ("ed", ""), ("ing", ""),
            ("er", ""), ("est", ""), ("s", ""), ("d", "")]

def _lemma(w):
    for suf, rep in _INFLECT:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            base = w[: -len(suf)] + rep
            yield base
            if suf in ("ed", "ing", "er", "est"):
                yield base + "e"
                if len(base) > 2 and base[-1] == base[-2]:
                    yield base[:-1]

def pos_of(word: str):
    """优先查词库（含词形还原），其次按后缀推断。"""
    w = word.lower().strip("-'")
    if w in _WORD2POS:
        return _WORD2POS[w]
    for base in _lemma(w):
        if base in _WORD2POS:
            return _WORD2POS[base]
    if w.endswith("ly") and len(w) > 5:
        return "adv"
    for bases, pos in _SUFFIX_POS:
        for b in bases:
            if w.endswith(b) and len(w) > len(b) + 3:
                return pos
    # 兜底：长词默认按名词处理（绝大多数英文长词是名词化形式）
    if len(w) >= 6:
        return "n"
    return None


def pos_strict(word: str):
    """只返回有把握的词性；用于出题，宁缺毋滥。"""
    w = word.lower().strip("-'")
    if w in _WORD2POS:
        return _WORD2POS[w]
    for base in _lemma(w):
        if base in _WORD2POS:
            return _WORD2POS[base]
    if w.endswith("ly") and len(w) > 5 and w[:-2] in _WORD2POS:
        return "adv"
    for bases, pos in _SUFFIX_POS:
        for b in bases:
            if w.endswith(b) and len(w) > len(b) + 4:
                return pos
    return None

def pool(pos: str):
    return sorted(_BY_POS.get(pos, ()))

def is_easy(word: str) -> bool:
    """多层还原：meetings -> meeting -> meet 也要能认出来。"""
    w = word.lower().strip("-'")
    seen = set()
    for _ in range(3):
        if not w or w in seen:
            break
        seen.add(w)
        if w in EASY:
            return True
        for suf, rep in (("ies", "y"), ("ied", "y"), ("ier", "y"), ("iest", "y"),
                         ("es", ""), ("ed", ""), ("ing", ""), ("s", ""), ("d", ""),
                         ("ly", "")):
            if not w.endswith(suf) or len(w) - len(suf) < 2:
                continue
            base = w[: -len(suf)] + rep
            if base in EASY:
                return True
            if suf in ("ed", "ing", "er", "est") and len(base) > 2 and base[-1] == base[-2]:
                if base[:-1] in EASY:
                    return True
            w = base
            break
        else:
            break
    return False

def is_cet6(word: str) -> bool:
    return word.lower() in _WORD2POS

def size() -> int:
    return len(_WORD2POS)
