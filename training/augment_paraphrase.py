"""用本地 30B 给训练问题做同义改写，扩句式多样性。

## 为什么需要
模板合成的问题句式只有 3~4 种，模型学到的是"句式 → 工具"的查表。
实测：加入改写数据后 OOD 从 48.8% → 83.1%。

## 三条硬约束（都踩过）
1. **OOD 留出的模板不能当种子**，否则测试集被污染。早期版本漏了这条，
   造成 2.9% 近似重复（6/207），已用 `--clean` 清洗。
2. **只让模型改措辞，不让它定标签**——标签继承原意图，避免标注错误。
3. 生成句里出现工具名/参数名的直接丢弃，否则模型学会作弊。

## 用法
    # 全量补一轮：每个工具各 4 个种子
    python training/augment_paraphrase.py --per-tool 4 --append

    # 只补弱工具
    python training/augment_paraphrase.py --tools knowledge_search,fetch_url --per-tool 6 --append

    # 清洗与测试集近似重复的样本
    python training/augment_paraphrase.py --clean
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

import httpx

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ / "training"))

from build_dataset import SPECS, TOOL_NAMES, build_ood  # noqa: E402

OUT = Path(__file__).resolve().parent / "data" / "paraphrase.jsonl"
CLS_DIR = Path(__file__).resolve().parent / "data_cls"
API = "http://127.0.0.1:8080/v1/chat/completions"
SEED = 20261101

PROMPT = """把下面这句话换 {n} 种说法，要求：
1. 意思完全不变（还是同一个需求）
2. 口语化、长短不一，像真人随口说的
3. 每行一句，不要编号，不要解释，不要引号
4. 不要出现工具名（如 read_file、web_search）

原句：{q}
"""


def ood_templates() -> set[str]:
    """OOD 测试集留出的模板 id —— 这些模板的问题绝不能当种子。"""
    _pairs, keep = build_ood()
    return {tid for tid, *_ in SPECS} - keep


def gen_once(q: str, n: int, temp: float = 0.9) -> list[str]:
    body = {
        "model": "default_model",
        "messages": [{"role": "user", "content": PROMPT.format(n=n, q=q)}],
        "temperature": temp,
        "max_tokens": 400,
    }
    try:
        r = httpx.post(API, json=body, timeout=180)
        if r.status_code != 200:
            return []
        text = r.json()["choices"][0]["message"]["content"]
    except Exception:                                     # noqa: BLE001
        return []

    lines = []
    for line in text.splitlines():
        s = line.strip().lstrip("-•*0123456789.、) ").strip().strip("“”\"'")
        if not 4 <= len(s) <= 60:
            continue
        if any(t in s for t in TOOL_NAMES):               # 防作弊：句子里不许出现工具名
            continue
        lines.append(s)
    return lines[:n]


def pick_seeds(per_tool: int, total: int, only: set[str], slots: int) -> list[tuple[str, str]]:
    banned = ood_templates()
    rng = random.Random(SEED)
    seeds: list[tuple[str, str]] = []
    for tid, tool, phrasings, slots_all, _b in SPECS:
        if tid in banned:                                 # 关键：不碰留出模板
            continue
        if only and tool not in only:
            continue
        cand = []
        for slot in slots_all[:slots]:
            for ph in phrasings:
                cand.append(ph.format(x=slot))
        rng.shuffle(cand)
        take = per_tool or 2
        seeds += [(q, tool) for q in cand[:take]]
    rng.shuffle(seeds)
    return seeds[:total] if total else seeds


def clean() -> None:
    """删掉与测试集近似重复的改写（防 OOD 污染）。"""
    import difflib

    norm = lambda s: re.sub(r"[\s，,。?？!！]", "", s)      # noqa: E731
    held: set[str] = set()
    for f in ("test_ood.jsonl", "test.jsonl"):
        p = CLS_DIR / f
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            held.add(norm(json.loads(line)["messages"][1]["content"]))

    rows = [json.loads(l) for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()]
    keep, drop = [], []
    for r in rows:
        nq = norm(r["q"])
        if any(difflib.SequenceMatcher(None, nq, h).ratio() >= 0.85 for h in held):
            drop.append(r)
        else:
            keep.append(r)
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep),
                   encoding="utf-8")
    print(f"清洗完成：保留 {len(keep)} 条，删除近似重复 {len(drop)} 条")
    for r in drop[:8]:
        print("  删:", r["q"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=6, help="每句生成几种说法")
    ap.add_argument("--per-tool", type=int, default=0, help="每个工具取几个种子")
    ap.add_argument("--seed-tools", type=int, default=0, help="总共取几个种子（0=不限）")
    ap.add_argument("--slots", type=int, default=3, help="每个模板用前几个槽位")
    ap.add_argument("--temp", type=float, default=0.9)
    ap.add_argument("--tools", default="", help="只补这些工具，逗号分隔")
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--clean", action="store_true", help="只做清洗，不生成")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    if args.clean:
        clean()
        return

    only = {t.strip() for t in args.tools.split(",") if t.strip()}
    seeds = pick_seeds(args.per_tool, args.seed_tools, only, args.slots)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    seen = {q for q, _ in seeds}
    if args.append and out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["q"])
            except (json.JSONDecodeError, KeyError):
                continue

    done = 0
    with out_path.open("a" if args.append else "w", encoding="utf-8") as f:
        for i, (q, tool) in enumerate(seeds, 1):
            new = [s for s in gen_once(q, args.per, args.temp) if s not in seen]
            for s in new:
                seen.add(s)
                f.write(json.dumps({"q": s, "tool": tool, "src": q}, ensure_ascii=False) + "\n")
            f.flush()
            done += len(new)
            print(f"[{i}/{len(seeds)}] {q[:26]} → +{len(new)}（累计 {done}）", flush=True)

    print(f"完成：{done} 条 → {out_path}")


if __name__ == "__main__":
    main()
