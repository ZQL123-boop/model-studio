"""一次加载模型，跑完多个 adapter 的 test / OOD，输出一张对比表。

比逐条跑 eval_cls.py 省时间：模型只加载一次，两个切分连跑，
结果按 adapter 存成 JSON，方便写 RESULTS.md 时直接引用。

    python training/eval_all.py \
        --adapters "cls17=training/adapters_cls17,cls17b=training/adapters_cls17b" \
        --out training/eval_results.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_cls import DATA, normalize  # noqa: E402

SPLITS = {"test": "test.jsonl", "ood": "test_ood.jsonl"}


def run_split(model, tok, path: Path, max_tokens: int = 16):
    from mlx_lm import generate

    items = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    hit, unknown = 0, 0
    per_tool: dict[str, list[int]] = {}
    for it in items:
        gold = it["messages"][-1]["content"]
        try:
            prompt = tok.apply_chat_template(it["messages"][:2], add_generation_prompt=True,
                                             tokenize=False, enable_thinking=False)
        except TypeError:
            prompt = tok.apply_chat_template(it["messages"][:2], add_generation_prompt=True,
                                             tokenize=False)
        pred = normalize(generate(model, tok, prompt=prompt, max_tokens=max_tokens, verbose=False))
        ok = int(pred == gold)
        hit += ok
        unknown += int(pred is None)
        c = per_tool.setdefault(gold, [0, 0])
        c[0] += 1
        c[1] += ok
    return {"n": len(items), "acc": hit / len(items),
            "unknown": unknown / len(items),
            "per_tool": {t: {"n": c[0], "acc": c[1] / c[0]} for t, c in per_tool.items()}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/qwen3-1.7b-4bit")
    ap.add_argument("--adapters", required=True,
                    help='逗号分隔的 name=path，path 留空表示不挂 adapter')
    ap.add_argument("--out", default="training/eval_results.json")
    args = ap.parse_args()

    from mlx_lm import load

    specs = []
    for part in args.adapters.split(","):
        name, _, path = part.partition("=")
        specs.append((name.strip(), path.strip() or None))

    results: dict[str, dict] = {}
    for name, path in specs:
        print(f"\n=== {name} ({path or '无 adapter'}) ===", flush=True)
        model, tok = load(args.model, adapter_path=path)
        results[name] = {"adapter": path}
        for split, fn in SPLITS.items():
            r = run_split(model, tok, DATA / fn)
            results[name][split] = r
            print(f"  {split:<4} {r['n']:>4} 条  选对 {r['acc'] * 100:5.1f}%  "
                  f"不在表 {r['unknown'] * 100:4.1f}%", flush=True)
        del model

    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果 → {args.out}")

    # 汇总表：只列两个切分的总分
    print(f"\n{'adapter':<12}{'test':>8}{'OOD':>8}")
    for name, r in results.items():
        print(f"{name:<12}{r['test']['acc'] * 100:>7.1f}%{r['ood']['acc'] * 100:>7.1f}%")


if __name__ == "__main__":
    main()
