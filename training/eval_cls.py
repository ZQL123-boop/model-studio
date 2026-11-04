"""评估"18 分类"版路由器。

指标只有一个：**选对工具的比例**（外加"输出不在表里"的比例）。

为什么不搞模糊评分：这个任务的输出就是 18 个词之一，
对就是对、错就是错。任何"部分正确"的加权都会掩盖问题。
唯一例外：模型输出带了标点/空格/大小写差异，先做规范化再比。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data_cls"

TOOLS = ["read_file", "write_file", "edit_file", "list_dir", "find_files", "grep_code",
         "run_bash", "web_search", "fetch_url", "knowledge_search", "get_weather",
         "get_exchange_rate", "generate_image", "make_chart", "analyze_data",
         "office", "design", "set_goal"]


def normalize(text: str) -> str | None:
    """把模型输出映射到 18 个工具名之一；映射不上返回 None。"""
    t = (text or "").strip().lower()
    t = re.sub(r"[\s`'\"。，,：:;；!！?？<>|]+", "", t)
    if not t:
        return None
    for name in TOOLS:                       # 完全包含（防"工具是 read_file"这种）
        if name in t:
            return name
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/qwen2.5-0.5b-4bit")
    ap.add_argument("--adapter", default="")
    ap.add_argument("--split", default="test", choices=["test", "ood"])
    ap.add_argument("--max-tokens", type=int, default=16)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    from mlx_lm import generate, load

    print(f"加载 {args.model}" + (f" + {args.adapter}" if args.adapter else ""))
    model, tok = load(args.model, adapter_path=(args.adapter or None))

    stem = "test" if args.split == "test" else "test_ood"
    items = [json.loads(l) for l in
             (DATA / f"{stem}.jsonl").read_text(encoding="utf-8").splitlines()]

    hit = miss = unknown = 0
    per_tool: dict[str, list[int]] = {}
    bad_examples = []

    for it in items:
        gold = it["messages"][-1]["content"]
        try:
            prompt = tok.apply_chat_template(it["messages"][:2], add_generation_prompt=True,
                                             tokenize=False, enable_thinking=False)
        except TypeError:
            prompt = tok.apply_chat_template(it["messages"][:2], add_generation_prompt=True,
                                             tokenize=False)
        out = generate(model, tok, prompt=prompt, max_tokens=args.max_tokens, verbose=False)
        pred = normalize(out)

        ok = pred == gold
        hit += int(ok)
        miss += int(not ok)
        unknown += int(pred is None)
        pt = per_tool.setdefault(gold, [0, 0])
        pt[0] += 1
        pt[1] += int(ok)
        if not ok and args.verbose:
            bad_examples.append((it["messages"][1]["content"], gold, out.strip()[:40], pred))

    n = len(items)
    print(f"\n{args.split} 集：{n} 条")
    print(f"  选对      {hit:>4}  {hit / n * 100:.1f}%")
    print(f"  选错      {miss:>4}  {miss / n * 100:.1f}%")
    print(f"  不在工具表 {unknown:>3}  {unknown / n * 100:.1f}%（算在选错里）")

    print(f"\n{'工具':<20}{'条数':>5}{'准确':>8}")
    for t, (c, ok) in sorted(per_tool.items(), key=lambda kv: kv[1][1] / max(kv[1][0], 1)):
        print(f"{t:<20}{c:>5}{ok / c * 100:>7.0f}%")

    if bad_examples:
        print("\n错例：")
        for q, gold, raw, pred in bad_examples[:10]:
            print(f"  Q: {q[:38]}")
            print(f"     期望 {gold} / 原始输出 {raw!r} / 归一化 {pred}")


if __name__ == "__main__":
    main()
