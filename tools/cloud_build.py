# -*- coding: utf-8 -*-
"""云端出题入口：在 GitHub Actions 里跑，产出当天的套题与每日一文。

只做三件事，不做任何界面：
  1) 把 DEEPSEEK_API_KEY 注入设置（密钥只存在于 Actions 的 secret 里，不落仓库）；
  2) 调本地同一套生成流水线（engine.pipeline），质量规则与电脑版完全一致；
  3) 把结果复制到 out/ 目录，交给 workflow 提交上去。

用法：
    python tools/cloud_build.py                # 生成"今天"（按 TZ=Asia/Shanghai）
    python tools/cloud_build.py --date 2026-10-22
    python tools/cloud_build.py --no-reading    # 只出套题，不出每日一文
"""
import argparse
import datetime
import os
import shutil
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine import daily_reading, pipeline, store  # noqa: E402


def _cn_today():
    """按北京时间取日期。runner 上是 UTC，直接 date.today() 会差 8 小时。"""
    tz = os.environ.get("TZ", "")
    if "Shanghai" in tz or "Asia/Shanghai" in tz:
        return datetime.date.today().isoformat()
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).date().isoformat()


def _inject_key():
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        print("!! 没有 DEEPSEEK_API_KEY，将退回本地引擎（不调模型，质量会低很多）")
        return False
    st = store.settings()
    llm = dict(st.get("llm") or {})
    llm.update({"enabled": True,
                "api_key": key,
                "base_url": os.environ.get("DEEPSEEK_BASE_URL") or llm.get("base_url")
                            or "https://api.deepseek.com/v1",
                "model": os.environ.get("DEEPSEEK_MODEL") or llm.get("model") or "deepseek-chat"})
    store.save_settings({"llm": llm})
    print("· 模型已就绪：%s" % llm["model"])
    return True


def _copy_out(date_str):
    out_sets = os.path.join(ROOT, "out", "sets")
    out_read = os.path.join(ROOT, "out", "reading")
    os.makedirs(out_sets, exist_ok=True)
    os.makedirs(out_read, exist_ok=True)
    got = []

    src = os.path.join(store.SETS, date_str + ".json")
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(out_sets, date_str + ".json"))
        got.append("out/sets/%s.json" % date_str)

    src = os.path.join(store.DATA, "reading", "daily-%s.json" % date_str)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(out_read, "daily-%s.json" % date_str))
        got.append("out/reading/daily-%s.json" % date_str)
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--no-reading", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    date_str = args.date or _cn_today()
    print("· 目标日期：%s" % date_str)
    _inject_key()

    ok, info = pipeline.build_today(force=args.force, set_id=date_str)
    for line in (pipeline.STATUS.get("log") or []):
        print("  " + line)
    if not ok:
        print("!! 出题失败：%s" % ((info or {}).get("error") or "未知"))
        return 1

    if not args.no_reading:
        try:
            daily_reading.ensure(date_str, background=False)
        except Exception:
            print("!! 每日一文生成失败：\n" + traceback.format_exc()[-500:])

    got = _copy_out(date_str)
    print("· 产出：" + ("、".join(got) if got else "（无，检查 data/sets）"))
    return 0 if got else 2


if __name__ == "__main__":
    sys.exit(main())
