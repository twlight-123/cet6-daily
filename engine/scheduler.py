"""程序内的每日更新调度。

以前靠外部定时任务，程序关着也能出题；现在收进程序里，规则是：

  · 打开程序时，若当天还没有套题、且已过设定时间（默认 07:00），立刻生成；
  · 程序一直开着时，每分钟看一眼，到点自动生成；
  · 生成失败不硬扛：20 分钟后重试，一天最多 3 次，失败原因记进 data/scheduler.json，
    页面上能看到，不会静默失败。

不需要任何外部服务，也不需要程序以外的进程。
"""
import datetime
import os
import re
import threading
import time

from . import pipeline, store

POLL_SECONDS = 60
RETRY_MINUTES = 20
MAX_TRIES = 3

_state_lock = threading.Lock()
_thread = None


def _state_path():
    return os.path.join(store.DATA, "scheduler.json")


def _load_state():
    return store.load_json(_state_path(), None) or {"date": "", "tries": 0,
                                                    "last_attempt": "", "error": ""}


def _save_state(st):
    store.save_json(_state_path(), st)


def status():
    st = _load_state()
    cfg = store.settings().get("auto", {})
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "hour": int(cfg.get("hour", 6)),
        "minute": int(cfg.get("minute", 0)),
        "date": st.get("date"),
        "tries": st.get("tries", 0),
        "last_attempt": st.get("last_attempt"),
        "error": st.get("error") or "",
        "have_today": bool(store.load_set(datetime.date.today().isoformat())),
    }


def _due(now, cfg):
    hour, minute = int(cfg.get("hour", 6)), int(cfg.get("minute", 0))
    if now.hour > hour:
        return True
    if now.hour == hour and now.minute >= minute:
        return True
    # 凌晨到设定时间之间不算"过期"，避免一开程序就补昨天的
    return False


AHEAD_DAYS = 3       # 往后备几天：关机三天内开机都还有题可做
PREP = os.path.join(store.DATA, "prep")      # 备题区：未来日期的内容先藏在这里


def _prep_path(rel, fn):
    return os.path.join(PREP, rel, fn)


def _stash(date_str):
    """把刚生成好的「未来日期」内容挪进备题区。

    未来那几天的套题如果留在 data/sets/ 里，程序里就会列出「22 号、23 号」
    这种还没到的日子。所以先藏进 data/prep/，等真到了那天再自动放出来。
    """
    n = 0
    pairs = (("sets", date_str + ".json"),
             ("reading", "daily-%s.json" % date_str))
    for rel, fn in pairs:
        src = os.path.join(store.DATA, rel, fn)
        if not os.path.exists(src):
            continue
        dst = _prep_path(rel, fn)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            os.replace(src, dst)
            n += 1
        except OSError as e:
            pipeline._log("备题存放失败 %s：%r" % (fn, e))
    return n


def promote():
    """把已经到日子的备题放回正式目录。

    常驻进程要能跨零点自动生效，所以 tick 每分钟、以及每次打开页面都会调一遍。
    返回放回来的文件名列表。
    """
    today = datetime.date.today().isoformat()
    moved = []
    if not os.path.isdir(PREP):
        return moved
    for rel in ("sets", "reading"):
        d = os.path.join(PREP, rel)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            m = re.search(r"(\d{4}-\d{2}-\d{2})", fn)
            if not m or m.group(1) > today:
                continue                      # 还没到日子，继续藏着
            src = os.path.join(d, fn)
            dst = os.path.join(store.DATA, rel, fn)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):
                    os.remove(src)
                else:
                    os.replace(src, dst)
                moved.append(fn)
            except OSError as e:
                pipeline._log("备题放出失败 %s：%r" % (fn, e))
    if moved:
        pipeline._log("备题启用：%s" % "、".join(moved))
    return moved


def _prepared(date_str):
    return (bool(store.load_set(date_str))
            or os.path.exists(_prep_path("sets", date_str + ".json")))


def ensure_ahead(days=AHEAD_DAYS):
    """把未来几天的题也备好（备好就藏进 prep/，列表里看不见）。

    电脑关机时本机程序跑不了，这是物理限制。退一步的办法是提前把明天的
    也生成好——这样今晚关机、明早开机，打开就有题，不用等两分半。
    已经有的跳过，只补缺的。
    """
    today = datetime.date.today()
    done = []
    for i in range(1, days + 1):
        d = (today + datetime.timedelta(days=i)).isoformat()
        if _prepared(d):
            continue
        pipeline._log("提前备题：%s" % d)
        ok, info = pipeline.build_today(force=False, set_id=d)
        if not ok:
            pipeline._log("提前备题失败 %s：%s" % (d, (info or {}).get("error") or "未知"))
            break
        _stash(d)
        done.append(d)
    return done



def _should_run():
    cfg = store.settings().get("auto", {})
    if not cfg.get("enabled", True):
        return False, "已关闭自动更新"
    now = datetime.datetime.now()
    today = now.date().isoformat()
    if store.load_set(today):
        return False, "今天已有内容"
    if not _due(now, cfg):
        return False, "还没到设定时间"
    if pipeline.STATUS.get("state") == "running":
        return False, "正在生成"

    st = _load_state()
    if st.get("date") == today:
        if st.get("tries", 0) >= MAX_TRIES:
            return False, "今天已重试 %d 次，仍失败" % st["tries"]
        last = st.get("last_attempt") or ""
        if last:
            try:
                gap = (now - datetime.datetime.fromisoformat(last)).total_seconds() / 60
                if gap < RETRY_MINUTES:
                    return False, "等待重试"
            except Exception:
                pass
    return True, "可以生成"


def tick(verbose=False):
    """检查一次。返回 (是否触发了生成, 原因)。"""
    try:
        promote()          # 跨零点后把当天备题放出来（常驻进程全靠这一步）
    except Exception:
        pass
    ok, why = _should_run()
    if not ok:
        if verbose:
            pipeline._log("自动更新跳过：%s" % why)
        return False, why

    now = datetime.datetime.now()
    today = now.date().isoformat()
    with _state_lock:
        st = _load_state()
        if st.get("date") != today:
            st = {"date": today, "tries": 0, "last_attempt": "", "error": ""}
        st["tries"] = st.get("tries", 0) + 1
        st["last_attempt"] = now.isoformat(timespec="seconds")
        _save_state(st)

    pipeline._log("自动更新：开始生成 %s 的内容（第 %d 次）" % (today, st["tries"]))

    def _run():
        ok2, info = pipeline.build_today(force=False)
        with _state_lock:
            s2 = _load_state()
            if ok2:
                s2["error"] = ""
            else:
                s2["error"] = (info or {}).get("error") or "未知错误"
            _save_state(s2)
        if ok2:
            try:
                ensure_ahead()
            except Exception as e:
                pipeline._log("提前备题出错：%r" % e)

    threading.Thread(target=_run, daemon=True).start()
    return True, "已启动"


def _loop():
    while True:
        try:
            tick()
        except Exception:
            pass
        time.sleep(POLL_SECONDS)


def start():
    """幂等：重复调用只会有一个后台线程。"""
    global _thread
    if _thread and _thread.is_alive():
        return _thread
    _thread = threading.Thread(target=_loop, daemon=True, name="daily-update")
    _thread.start()
    return _thread
