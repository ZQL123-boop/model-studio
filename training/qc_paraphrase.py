"""按 PROMPT.md 第三节给 30B 改写数据做质检。

抓的是「标签对但句子废」的样本：
    重复 / 过短 / 过长 / 含工具名或参数名 / 疑似意图漂移

规则可复现、不调模型；默认只报告，--drop 写出剔除**硬问题**后的副本
（意图漂移只报不删——改写句丢掉路径/链接有时仍然合法，需要人看）。

    python training/qc_paraphrase.py
    python training/qc_paraphrase.py --drop     # → data/paraphrase.clean.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_dataset import TOOL_NAMES  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
SRC = DATA / "paraphrase.jsonl"

# PROMPT.md：句子里出现工具名或参数名就算脏（模型会学到作弊）
LEAK = list(TOOL_NAMES) + ["query", "args", "tool_call", "工具名"]

NEAR_DUP = 0.85          # 3-gram Jaccard 超过这个数当重复
MIN_LEN, MAX_LEN = 5, 60


def norm(s: str) -> str:
    return re.sub(r"[\s，。、！？；：!?,.;:　\"'“”‘’（）()【】\[\]]+", "", s)


def grams(s: str) -> set[str]:
    s = norm(s)
    return {s[i:i + 3] for i in range(len(s) - 2)} or {s}


def jaccard(a: str, b: str) -> float:
    ga, gb = grams(a), grams(b)
    return len(ga & gb) / max(len(ga | gb), 1)


def has_path(s: str) -> bool:
    return bool(re.search(r"[~/][\w.\-/]+|\b[\w\-]+\.(?:md|txt|py|js|csv|xlsx|docx|pptx|png|jpe?g|pdf)\b", s))


def has_url(s: str) -> bool:
    return bool(re.search(r"https?://|www\.|\b[\w-]+\.(?:com|cn|org|net)\b", s))


def check(rows: list[dict]) -> list[dict]:
    """返回每条的问题列表；没问题的条目不会出现在结果里。"""
    issues: list[dict] = []
    kept: list[str] = []                     # 已通过的去重基准
    for i, d in enumerate(rows):
        q = (d.get("q") or "").strip()
        hard: list[str] = []
        soft: list[str] = []

        if not q:
            hard.append("空")
        else:
            if len(q) < MIN_LEN:
                hard.append("过短")
            if len(q) > MAX_LEN:
                hard.append("过长")
            if any(t in q for t in LEAK):
                hard.append("含工具名")
            n = norm(q)
            if any(jaccard(q, k) >= NEAR_DUP for k in kept):
                hard.append("重复")
            src = d.get("src")
            if isinstance(src, str) and src:
                if has_url(src) and not has_url(q):
                    soft.append("疑似漂移:链接")
                if has_path(src) and not has_path(q):
                    soft.append("疑似漂移:路径")
            if not hard:
                kept.append(q)

        if hard or soft:
            issues.append({"i": i, "q": q, "tool": d.get("tool"), "hard": hard, "soft": soft})
    return issues


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--drop", action="store_true", help="写出剔除硬问题的副本")
    ap.add_argument("--show", type=int, default=8, help="每类打印几条样例")
    args = ap.parse_args()

    path = Path(args.src)
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    issues = check(rows)
    bad = {it["i"] for it in issues if it["hard"]}

    hard_cnt = Counter(x for it in issues for x in it["hard"])
    soft_cnt = Counter(x for it in issues for x in it["soft"])

    print(f"{path.name}: {len(rows)} 条")
    print(f"  硬问题 {len(bad)} 条  {dict(hard_cnt) or '{}'}")
    print(f"  软提醒 {sum(1 for it in issues if it['soft'])} 条  {dict(soft_cnt) or '{}'}")
    print(f"  按工具：{dict(Counter(it['tool'] for it in issues if it['hard'])) or '{}'}")

    for cat in list(hard_cnt) + list(soft_cnt):
        ex = [it for it in issues if cat in it["hard"] or cat in it["soft"]][:args.show]
        print(f"\n[{cat}] 例：")
        for it in ex:
            print(f"  #{it['i']:<4} {it['q'][:46]}")

    if args.drop:
        out = path.with_suffix(".clean.jsonl")
        kept = [r for i, r in enumerate(rows) if i not in bad]
        with out.open("w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\n写出 {out}：{len(kept)} 条（剔除 {len(bad)} 条）")


if __name__ == "__main__":
    main()
