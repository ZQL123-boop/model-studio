"""构建"18 分类"版数据集：只学选工具，不学填参数。

## 为什么要拆开

v2 的 JSON 版在 OOD 上 60%，但拆开看：
    web_search 选对 100%、query 只对 4.2%
    knowledge_search 选对 40%、query 0%
"会选不会填"——参数抽取是小模型的短板，而选工具是它的长项。

拆开之后：
    小模型：输入一句话 → 输出一个工具名（18 选 1，输出只有 3~20 token）
    参数  ：规则抽取（路径/URL/命令/城市）→ 抽不到再交给 30B

好处：
1. 输出极短 → 学得会、推理快（一次几十毫秒）
2. 准确率可解释：错就是选错，不存在"选对了但参数乱填"
3. 可以直接约束：输出不在工具表里就判"拒绝路由"

## 输出

    training/data_cls/{train,valid,test,test_ood}.jsonl
    每行 {"messages":[system, user, assistant]}，assistant 只有工具名
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_dataset import CATALOG, SPECS, TOOL_NAMES, build_ood, load_real  # noqa: E402

OUT = Path(__file__).resolve().parent / "data_cls"
PARA = Path(__file__).resolve().parent / "data" / "paraphrase.jsonl"   # 30B 生成的改写
SEED = 20261101
random.seed(SEED)

# 必须带上每个工具的一句话用途。只给名字的话，模型分不清
# fetch_url / read_file / web_search 的边界（实测 OOD 掉到 58.9%）
SYSTEM = (
    "你是工具路由器。根据用户请求，从下面选一个最合适的工具，"
    "只输出工具名，不要解释、不要标点：\n"
    + "\n".join(f"{n}：{d}" for n, d in CATALOG)
)


def msg(q: str, tool: str) -> dict:
    return {"messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": q},
        {"role": "assistant", "content": tool},
    ]}


def synth_pairs() -> list[tuple[str, str, str]]:
    """(问题, 工具, 模板id)。"""
    out = []
    for tid, tool, phrasings, slots, _b in SPECS:
        for slot in slots:
            for tpl in phrasings:
                out.append((tpl.format(x=slot), tool, tid))
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    all_synth = synth_pairs()
    ood_pairs, keep_tids = build_ood()
    ood_q = {q for q, _t in ood_pairs}

    # 训练池：排除 OOD 模板
    pool = [(q, t, "synth") for q, t, tid in all_synth if tid in keep_tids and q not in ood_q]
    # 真实样本全进训练侧（只有 34 条，不拿来测）
    pool += [(q, t["tool"], "real") for q, t in load_real()]

    para_items: list[tuple[str, str, str]] = []
    # 30B 生成的口语化改写：只进训练池。标签来自原意图，改写只换措辞，
    # 所以不会引入错误标注；这一步专门解决"句式只有模板那几种"的问题。
    if PARA.exists():
        n_para = 0
        for line in PARA.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            q, tool = (d.get("q") or "").strip(), d.get("tool")
            if q and tool in TOOL_NAMES:
                para_items.append((q, tool, "para"))
                n_para += 1
        print(f"并入 30B 改写样本 {n_para} 条")

    # 按工具分层：80/10/10
    by_tool: dict[str, list[tuple[str, str, str]]] = {}
    for q, tool, src in pool:
        by_tool.setdefault(tool, []).append((q, tool, src))

    train, valid, test = [], [], []
    per_tool = {}
    for tool, items in sorted(by_tool.items()):
        seen, uniq = set(), []
        for q, t, s in items:
            k = re.sub(r"\s+", "", q)[:80]
            if k in seen:
                continue
            seen.add(k)
            uniq.append((q, t, s))
        random.shuffle(uniq)
        n = len(uniq)
        n_test = max(2, round(n * 0.1))
        n_valid = max(1, round(n * 0.1))
        if n_test + n_valid > n * 0.4:
            n_test, n_valid = max(2, round(n * 0.08)), max(1, round(n * 0.08))
        test += [msg(q, t) for q, t, _ in uniq[:n_test]]
        valid += [msg(q, t) for q, t, _ in uniq[n_test:n_test + n_valid]]
        train += [msg(q, t) for q, t, _ in uniq[n_test + n_valid:]]
        per_tool[tool] = {"total": n, "train": n - n_test - n_valid, "test": n_test}

    # 改写只进 train，绝不参与 test/valid：同一句话的两种说法分居两侧会虚高分数
    # （旧版 test 103 条里有 37 条就是改写句，等价于泄漏）。
    if para_items:
        held = {re.sub(r"\s+", "", it["messages"][1]["content"])[:80] for it in test + valid}
        added = 0
        for q, tool, _s in para_items:
            k = re.sub(r"\s+", "", q)[:80]
            if k in held:
                continue
            held.add(k)
            train.append(msg(q, tool))
            added += 1
        print(f"改写并入 train {added} 条（不进 test/valid）")

    ood = [msg(q, t["tool"]) for q, t in ood_pairs]

    # 每工具实际 train 条数（含改写），stats 与"样本过薄"警告都按真实值
    from collections import Counter
    _cnt = Counter(it["messages"][-1]["content"] for it in train)
    for _t, _c in per_tool.items():
        _c["train"] = _cnt.get(_t, 0)

    random.shuffle(train)
    random.shuffle(test)

    def write(name, items):
        p = OUT / name
        with p.open("w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        return len(items)

    n_tr, n_va, n_te, n_ood = (write("train.jsonl", train), write("valid.jsonl", valid),
                               write("test.jsonl", test), write("test_ood.jsonl", ood))

    # 校验：标签必须是工具表里的名字
    bad = 0
    for name in ("train.jsonl", "valid.jsonl", "test.jsonl", "test_ood.jsonl"):
        for line in (OUT / name).read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            if d["messages"][-1]["content"] not in TOOL_NAMES:
                bad += 1
    if bad:
        raise SystemExit(f"[失败] {bad} 条标签非法")

    thin = [(t, c["train"]) for t, c in per_tool.items() if c["train"] < 20]
    if thin:
        print(f"[警告] 训练样本 <20 的工具：{thin}")

    (OUT / "stats.json").write_text(json.dumps(
        {"seed": SEED, "train": n_tr, "valid": n_va, "test": n_te, "ood": n_ood,
         "per_tool": per_tool}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"写出 train {n_tr} / valid {n_va} / test {n_te} / OOD {n_ood}")
    print(f"{'工具':<20}{'train':>7}{'test':>6}")
    for t, c in sorted(per_tool.items(), key=lambda kv: -kv[1]["train"]):
        print(f"{t:<20}{c['train']:>7}{c['test']:>6}")
    print("标签校验通过")


if __name__ == "__main__":
    main()
