"""评估工具路由器：基座 vs 微调后。

指标（三个，缺一不可）：
1. **JSON 合法率** —— 输出能不能解析成 {"tool","args"}
2. **工具名准确率** —— 选的工具对不对
3. **参数命中率** —— 关键参数（query/path/command/action）值对不对

为什么要分开报：只报工具名准确率会掩盖"工具对、参数乱编"的问题，
那种模型上线后一样不能用。

为什么要分 real / synth：合成测试集考的是"模板泛化"，
真实测试集考的是"实际使用" —— 后者只有 7 条，数字只能当参考，不能当结论。

用法：
    python training/eval_router.py                       # 基座
    python training/eval_router.py --adapter training/adapters  # 微调后
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

# 这些参数决定"调用得对不对"，其余参数次要
KEY_ARG = {
    "web_search": "query", "fetch_url": "url", "knowledge_search": "query",
    "read_file": "path", "write_file": "path", "edit_file": "path",
    "list_dir": "path", "find_files": "pattern", "grep_code": "pattern",
    "run_bash": "command", "get_weather": "city", "get_exchange_rate": "base",
    "generate_image": "prompt", "make_chart": "spec", "analyze_data": "path",
    "office": "action", "design": "action", "set_goal": "goal",
}


def extract_json(text: str) -> dict | None:
    """从模型输出里抠出第一个能解析的 JSON 对象。

    不能用正则 `\\{.*?\\}`：模型输出的是多行嵌套 JSON，
    非贪婪匹配会在内层 `}` 就截断，解析必然失败——
    这会让基线"看起来 0 分"，把评估结论带偏（踩过）。
    正确做法：括号配对扫描，逐层找完整的对象。
    """
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()

    # 1) 整段就是 JSON
    try:
        obj = json.loads(t)
        if isinstance(obj, dict) and "tool" in obj:
            return obj
    except json.JSONDecodeError:
        pass

    # 2) 括号配对：从每个 `{` 出发找配对的那个 `}`
    for start in range(len(t)):
        if t[start] != "{":
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(t[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict) and "tool" in obj:
                        return obj
                    break
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/qwen2.5-0.5b-4bit")
    ap.add_argument("--adapter", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-tokens", type=int, default=120)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--split", default="test", choices=["test", "ood"],
                    help="test=同句式分层切分；ood=问法没见过的泛化测试")
    args = ap.parse_args()

    from mlx_lm import generate, load

    print(f"加载 {args.model}" + (f" + adapter {args.adapter}" if args.adapter else ""))
    model, tok = load(args.model, adapter_path=(args.adapter or None))

    stem = "test" if args.split == "test" else "test_ood"
    items = [json.loads(l) for l in (DATA / f"{stem}.jsonl").read_text(encoding="utf-8").splitlines()]
    metas = [json.loads(l) for l in (DATA / f"{stem}_meta.jsonl").read_text(encoding="utf-8").splitlines()]
    if args.limit:
        items, metas = items[: args.limit], metas[: args.limit]

    stat: dict[str, list[int]] = {}                     # src -> [合法, 工具对, 参数对]
    total = [0, 0, 0]
    per_tool: dict[str, list[int]] = {}                 # tool -> [n, tool_ok, arg_ok]
    off_catalog = 0
    fails = []

    for it, meta in zip(items, metas):
        msgs = it["messages"][:2]                        # system + user
        # Qwen3 默认先输出 <think>，路由任务不需要；Qwen2.5 模板不认这个参数，
        # 所以先试带参、失败再退回（否则换模型就得改代码）
        try:
            prompt = tok.apply_chat_template(msgs, add_generation_prompt=True,
                                             tokenize=False, enable_thinking=False)
        except TypeError:
            prompt = tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
        out = generate(model, tok, prompt=prompt, max_tokens=args.max_tokens, verbose=False)

        gold = json.loads(it["messages"][-1]["content"])
        pred = extract_json(out)
        src = meta["src"]

        ok_json = pred is not None
        ok_tool = bool(pred and pred.get("tool") == gold["tool"])
        ok_arg = False
        if ok_tool:
            key = KEY_ARG.get(gold["tool"])
            if key:
                # 模型可能把 args 写成数组/字符串，别直接 .get 炸掉
                praw = pred.get("args")
                pargs = praw if isinstance(praw, dict) else {}
                gargs = gold["args"] if isinstance(gold["args"], dict) else {}
                g, pr = gargs.get(key), pargs.get(key)
                ok_arg = g is not None and pr is not None and str(g).strip() == str(pr).strip()
            else:
                ok_arg = True

        if pred and pred.get("tool") not in KEY_ARG:
            off_catalog += 1

        arr_src = stat.setdefault(src, [0, 0, 0])
        for arr in (arr_src, total):
            arr[0] += int(ok_json)
            arr[1] += int(ok_tool)
            arr[2] += int(ok_arg)

        pt = per_tool.setdefault(gold["tool"], [0, 0, 0])
        pt[0] += 1
        pt[1] += int(ok_tool)
        pt[2] += int(ok_arg)

        if not ok_tool and args.verbose:
            fails.append((it["messages"][1]["content"], gold, pred))

    # 分组条数
    cnt: dict[str, int] = {}
    for m in metas:
        cnt[m["src"]] = cnt.get(m["src"], 0) + 1
    cnt["all"] = len(items)

    def pct(a, b):
        return f"{a / b * 100:.1f}%" if b else "—"

    print("\n测试集：" + " / ".join(f"{k} {v} 条" for k, v in cnt.items() if k != "all") + "\n")
    print(f"{'分组':<6}{'条数':>5}{'JSON合法':>10}{'工具准确':>10}{'参数命中':>10}")
    for k, arr in list(stat.items()) + [("all", total)]:
        c = cnt.get(k, 0)
        print(f"{k:<6}{c:>5}{pct(arr[0], c):>10}{pct(arr[1], c):>10}{pct(arr[2], c):>10}")

    # 按工具拆：看清"哪些工具学会了、哪些完全没学会"
    print(f"\n{'工具':<20}{'条数':>5}{'工具准确':>10}{'参数命中':>10}")
    for tool, (n, tok_ok, arg_ok) in sorted(per_tool.items(), key=lambda kv: kv[1][1] / max(kv[1][0], 1)):
        print(f"{tool:<20}{n:>5}{pct(tok_ok, n):>10}{pct(arg_ok, n):>10}")

    if off_catalog:
        print(f"\n⚠️ 输出不在工具表里的次数：{off_catalog}（这些一律判错）")

    if fails:
        print("\n判错的例子：")
        for q, gold, pred in fails[:8]:
            print(f"  Q: {q[:40]}")
            print(f"     期望 {gold['tool']} / 得到 {(pred or {}).get('tool') if pred else '无法解析'}")


if __name__ == "__main__":
    main()
