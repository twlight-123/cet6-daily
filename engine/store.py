"""数据持久化（JSON，原子写）。"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_CODE_ROOT = os.path.dirname(_HERE)


def _frozen():
    return bool(getattr(sys, "frozen", False))


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".cet6_write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


def _frozen_root():
    """决定数据落在哪。

    单文件 exe 双击时会把自身解到临时目录（_MEIPASS，退出即清），
    所以套题/进度/生词本绝不能写在那里。规则：
      1) exe 所在目录可写 → 就地放 data/（便携，整个目录拷走即搬家）
      2) 只读位置（Program Files 之类）→ 落到用户目录，不至于闪退
    """
    base = os.path.dirname(os.path.abspath(sys.executable))
    if _writable(base):
        return base
    fallback = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                            "CET6每日精练")
    try:
        os.makedirs(fallback, exist_ok=True)
    except Exception:
        pass
    return fallback


# 打包成 exe 后：代码和 web/、词表在 _MEIPASS（只读，退出即清），
# 而套题、进度、生词本这些必须写在 exe 旁边，否则一关就没了。
ROOT = _frozen_root() if _frozen() else _CODE_ROOT
DATA = os.path.join(ROOT, "data")
SETS = os.path.join(DATA, "sets")
PROGRESS = os.path.join(DATA, "progress.json")
SETTINGS = os.path.join(DATA, "settings.json")
LOG = os.path.join(DATA, "update.log")

_lock = threading.RLock()

DEFAULT_SETTINGS = {
    "llm": {
        "enabled": False,
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "timeout": 120,
    },
    "audio": {"rate": 1.0, "voice": ""},
    "exam": {"onePassListening": False},
    # 程序内每日自动更新（不开程序就不跑）
    "auto": {"enabled": True, "hour": 6, "minute": 0},
}

DEFAULT_PROGRESS = {"attempts": [], "wrong": {}, "vocab": {}, "seen": []}


def ensure_dirs():
    os.makedirs(SETS, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)


def _atomic_write(path, obj):
    ensure_dirs()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    with _lock:
        _atomic_write(path, obj)


def settings():
    s = load_json(SETTINGS, None)
    if not s:
        save_json(SETTINGS, DEFAULT_SETTINGS)
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    for k, v in s.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def save_settings(patch: dict):
    cur = settings()
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(cur.get(k), dict):
            cur[k].update(v)
        else:
            cur[k] = v
    save_json(SETTINGS, cur)
    return cur


def progress():
    p = load_json(PROGRESS, None)
    if not p:
        save_json(PROGRESS, DEFAULT_PROGRESS)
        return json.loads(json.dumps(DEFAULT_PROGRESS))
    for k, v in DEFAULT_PROGRESS.items():
        p.setdefault(k, json.loads(json.dumps(v)))
    return p


def save_progress(p):
    save_json(PROGRESS, p)


def locked():
    """进度的读-改-写要包在锁里（RLock，可重入）。

    服务是多线程的：判分、查词、生词本可能同时在改 progress.json，
    各自读一份旧值再写回，后写的会把先写的整段覆盖掉——错题偶尔
    「自己没了」就是这么来的。
    """
    return _lock


def set_path(set_id):
    return os.path.join(SETS, "%s.json" % set_id)


def load_set(set_id):
    return load_json(set_path(set_id), None)


def list_sets():
    ensure_dirs()
    today = datetime.date.today().isoformat()
    out = []
    for fn in os.listdir(SETS):
        if not fn.endswith(".json"):
            continue
        d = load_json(os.path.join(SETS, fn), None)
        if not d:
            continue
        # 未来日期的套题不列出来：预览/备题不该出现在「今天做哪套」里
        if (d.get("id") or "") > today:
            continue
        sections = d.get("sections", [])
        out.append({
            "id": d.get("id"),
            "date": d.get("date"),
            "title": d.get("title"),
            "source": d.get("source"),
            "engine": d.get("engine"),
            "topics": d.get("topics", []),
            "sections": [{"id": s["id"], "type": s["type"], "title": s["title"],
                          "count": len(s.get("questions", [])) or len(s.get("bank", [])),
                          "blank_count": s.get("blank_count") or len(s.get("answers", [])),
                          "bank": len(s.get("bank", []))}
                         for s in sections],
            "minutes": d.get("minutes", 30),
            "difficulty": _difficulty_summary(d),
        })
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out


def _difficulty_summary(d):
    """按六级标尺现算，人工写的和自动生成的都一视同仁。"""
    try:
        from . import difficulty
        chk = difficulty.check_set(d)
        return {"score": chk["score"], "level": chk["level"], "in_band": chk["in_band"]}
    except Exception:
        return None


def _seed_from_bundle():
    """首次以 exe 运行：把内置的初始题库、文章、词表复制到 exe 旁边。"""
    if not _frozen():
        return
    src = os.path.join(_CODE_ROOT, "data")
    if os.path.abspath(src) == os.path.abspath(DATA):
        return
    if os.path.isdir(DATA) and os.listdir(DATA):
        return
    try:
        shutil.copytree(src, DATA, dirs_exist_ok=True)
    except Exception:
        pass


_seed_from_bundle()
