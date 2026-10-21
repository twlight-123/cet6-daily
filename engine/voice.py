# -*- coding: utf-8 -*-
"""神经语音合成（微软 Edge 在线语音），合成结果按句缓存成 mp3。

为什么不用浏览器 speechSynthesis：
  - Windows 内置音色是拼接式合成，机械感很重，听感像导航播报；
  - 更致命的是浏览器默认只给一个 voice，对话里的男女声会变成同一个人。

这里给 W / M / N 三个说话人各配一条神经语音，逐句合成、落盘缓存，
前端按句播放 mp3。失败时抛 VoiceUnavailable，前端自动退回浏览器语音。
"""
import asyncio
import hashlib
import os
import threading

from . import store

TTS_DIR = os.path.join(store.DATA, "tts")

# 说话人 -> 音色。Ava / Andrew / Brian 属于较新的多语言神经语音，语流自然。
VOICES = {
    "W": "en-US-AvaNeural",
    "M": "en-US-AndrewNeural",
    "N": "en-US-BrianNeural",
}
# 万一上面这些在旧版 edge-tts 里不存在，用这些兜底
FALLBACK = {
    "W": "en-US-AriaNeural",
    "M": "en-US-GuyNeural",
    "N": "en-US-GuyNeural",
}


class VoiceUnavailable(Exception):
    """合成不可用（没装 edge-tts、断网、音色不可用等）。"""


_locks = {}
_locks_guard = threading.Lock()
_disabled = False


def _lock_for(key):
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


def is_available():
    """本机是否具备合成能力（只判断模块，不联网）。"""
    if _disabled:
        return False
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False


def voice_for(sp):
    return VOICES.get((sp or "N").upper(), VOICES["N"])


def _rate_str(rate):
    """1.0 -> "+0%"，1.15 -> "+15%"。"""
    try:
        pct = int(round((float(rate) - 1.0) * 100))
    except Exception:
        pct = 0
    pct = max(-40, min(50, pct))
    return "%+d%%" % pct


def _cache_path(text, sp, rate):
    voice = voice_for(sp)
    raw = "%s|%s|%s" % (voice, _rate_str(rate), text)
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    return os.path.join(TTS_DIR, "%s.mp3" % h)


async def _synth(text, voice, rate, out):
    import edge_tts
    comm = edge_tts.Communicate(text, voice, rate=rate)
    await comm.save(out)


def get_audio(text, sp="N", rate=1.0):
    """返回 mp3 路径；必要时合成。线程安全，同句并发只合成一次。"""
    text = (text or "").strip()
    if not text:
        raise VoiceUnavailable("空文本")
    if not is_available():
        raise VoiceUnavailable("未安装 edge-tts")

    os.makedirs(TTS_DIR, exist_ok=True)
    path = _cache_path(text, sp, rate)
    if os.path.exists(path) and os.path.getsize(path) > 800:
        return path

    global _disabled
    with _lock_for(path):
        if os.path.exists(path) and os.path.getsize(path) > 800:
            return path
        tmp = path + ".part"
        last = None
        for voice in (voice_for(sp), FALLBACK.get((sp or "N").upper(), "en-US-GuyNeural")):
            try:
                asyncio.run(_synth(text, voice, _rate_str(rate), tmp))
                if os.path.exists(tmp) and os.path.getsize(tmp) > 800:
                    os.replace(tmp, path)
                    return path
                last = "输出为空"
            except Exception as e:
                last = e
            finally:
                if os.path.exists(tmp) and os.path.getsize(tmp) < 800:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
        # 连续失败：标记整体不可用，避免每次请求都卡十几秒
        _disabled = True
        raise VoiceUnavailable("合成失败：%s" % (last,))


def purge():
    """清掉所有缓存音频。"""
    n = 0
    if os.path.isdir(TTS_DIR):
        for fn in os.listdir(TTS_DIR):
            if fn.endswith(".mp3") or fn.endswith(".part"):
                try:
                    os.remove(os.path.join(TTS_DIR, fn))
                    n += 1
                except Exception:
                    pass
    return n
