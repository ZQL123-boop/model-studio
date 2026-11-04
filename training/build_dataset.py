"""构建"工具路由器"训练集（v2：按工具分层，修掉 v1 的致命缺陷）。

## v1 错在哪

v1 按"模板整组"切分，结果 `analyze_data` 在训练集里 **0 条**、测试集 7 条（15%）。
模型从没见过这个工具，那 7 条必然全错，准确率天花板被压到 85%。
`list_dir` 更惨：训练 1 条、测试 6 条。

## v2 怎么改

1. **按工具分层切分**：每个工具内部洗牌后 80/10/10，
   保证每个工具训练 ≥20 条、测试 2~3 条 —— 测试集只考"学过之后能不能区分"，
   不考"没见过能不能猜"（那是另一个实验，别混在一起）。
2. **每个工具凑到约 60 条**：多问法 × 多槽位组合，去重后统计。
3. **真实样本照收**：34 条全进训练侧（量太小，不拿来当测试集，否则 1 条错 = 14 pt 噪声）。

## 输出

    training/data/train.jsonl valid.jsonl test.jsonl
    training/data/test_meta.jsonl   （每行：src + tool，评估要按工具拆开看）
    training/data/stats.json
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
SESSIONS = PROJ / "data" / "agent_sessions"
OUT = Path(__file__).resolve().parent / "data"

SEED = 20261101
random.seed(SEED)

CATALOG = [
    ("read_file", "读本地文件内容"),
    ("write_file", "新建或覆盖文件"),
    ("edit_file", "精确替换文件中一段"),
    ("list_dir", "列目录"),
    ("find_files", "按文件名找"),
    ("grep_code", "按内容搜代码"),
    ("run_bash", "跑 shell 命令"),
    ("web_search", "联网搜索"),
    ("fetch_url", "抓网页正文"),
    ("knowledge_search", "查本地算法知识库"),
    ("get_weather", "查天气"),
    ("get_exchange_rate", "查汇率"),
    ("generate_image", "文生图"),
    ("make_chart", "把数据画成图"),
    ("analyze_data", "分析 CSV/JSON 结构"),
    ("office", "Office 文档：转 Word / 读写 Excel / PPT"),
    ("design", "检查网页布局 / 截图"),
    ("set_goal", "记录长期目标"),
]
TOOL_NAMES = [t[0] for t in CATALOG]
SYSTEM = (
    "你是工具路由器。根据用户请求，只输出一个 JSON，格式："
    '{"tool": "<工具名>", "args": {...}}\n'
    "可用工具：" + "、".join(f"{n}({d})" for n, d in CATALOG) + "\n"
    "不要解释，不要 markdown 代码块，只输出 JSON。"
)


def _msg(q: str, tool: str, args: dict) -> dict:
    return {"messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": q},
        {"role": "assistant", "content": json.dumps({"tool": tool, "args": args},
                                                    ensure_ascii=False)},
    ]}


# --------------------------------------------------------------------- 槽位池
# 每个模板 12~16 个槽位 × 3~4 种问法 ≈ 40~60 条，去重后按工具汇总
FILES = ["~/Desktop/笔记.md", "~/Desktop/MODEL_STUDIO_知识库.md", "~/Projects/sandbox/fib.py",
         "~/Projects/sandbox/README.md", "app/main.py", "app/tools.py", "data/memory.json",
         "~/Projects/sandbox/out.json", "~/Desktop/成绩.csv", "~/Desktop/报表.xlsx",
         "~/Projects/sandbox/landing.html", "config/settings.yaml", "logs/studio.err.log",
         "~/Desktop/我的记忆_模型版.md", "~/Projects/sandbox/test.md"]
DIRS = ["~/Projects", "~/Projects/sandbox", "~/Desktop", "~/Projects/model-studio",
        "~/Projects/model-studio/app", "~/.workbuddy", "data", "logs", "training", "models"]
EXTS = ["py", "md", "json", "yaml", "html", "ts", "sh", "csv"]
PATTERNS = ["lowbit", "addEventListener", "def run(", "TODO", "import os", "async def",
            "SELECT ", "fetch(", "useState", "panic("]
CMDS = ["ls -la", "date", "df -h", "python -V", "git status", "ps aux | grep python",
        "du -sh .", "uname -a", "curl -I https://example.com", "mkdir -p ./tmp",
        "wc -l training/data/train.jsonl", "cat /etc/hosts", "pip list", "node -v", "top -l 1"]
URLS = ["https://detail.zol.com.cn/vga/s11071/", "https://example.com",
        "https://www.ruanyifeng.com/blog/", "https://news.ycombinator.com",
        "https://docs.python.org/3/", "https://github.com/ml-explore/mlx",
        "https://pypi.org/project/matplotlib/", "https://www.apple.com/cn/"]
QUERIES = ["树状数组 lowbit", "线段树合并", "线性基", "KMP", "并查集按秩合并",
           "Dijkstra 堆优化", "快速幂", "莫队", "斜率优化 DP", "可持久化线段树",
           "Tarjan 强连通", "树链剖分", "势能分析", "二分图最大匹配"]
BRANDS = ["FastAPI", "Python", "Node.js", "React", "Qwen3", "Vue", "Rust", "Docker",
          "PyTorch", "MySQL", "Redis", "Nginx", "macOS", "MLX", "llama.cpp"]
CITIES = ["深圳", "中山", "北京", "上海", "广州", "成都", "杭州", "武汉", "西安", "南京", "珠海", "东莞"]
CURRENCIES = ["USD", "JPY", "EUR", "HKD", "GBP", "AUD", "CAD", "SGD", "KRW", "THB", "TWD", "CHF"]
IMG_SUBJ = ["赛博朋克风格的猫", "水墨山水", "九个头的小龙", "落日下的雪山", "机械朋克城市",
            "一只戴墨镜的柴犬", "星空下的营地", "水彩风格的港口", "赛博武士", "复古收音机",
            "玻璃质感的鲸鱼", "森林里的小木屋"]
CHART_SUBJ = ["近 7 天显卡价格", "每个月销量", "CPU 占用", "内存趋势", "每日步数",
              "季度营收", "气温变化", "训练 loss", "站点访问量", "电池健康度", "汇率走势", "文件大小分布"]
DATA_FILES = ["~/Projects/sandbox/_data_test/sales.csv", "~/Desktop/成绩.csv",
              "data/chat_history.jsonl", "~/Projects/sandbox/data.xlsx",
              "logs/studio.out.log", "~/Desktop/报表.xlsx", "~/Projects/sandbox/users.csv",
              "data/memory.json", "~/Desktop/订单.csv", "~/Projects/sandbox/metrics.json"]
MD_FILES = ["~/Desktop/笔记.md", "~/Desktop/MODEL_STUDIO_知识库.md", "~/Projects/sandbox/README.md",
            "~/Desktop/周报.md", "~/Projects/model-studio/training/README.md",
            "~/Desktop/会议记录.md", "~/Projects/sandbox/demo.md", "~/Desktop/ideas.md"]
HTML_FILES = ["~/Projects/sandbox/landing.html", "~/Desktop/index.html", "static/app.html",
              "~/Projects/sandbox/login.html", "~/Desktop/dashboard.html", "public/home.html",
              "~/Projects/sandbox/pricing.html", "~/Desktop/about.html", "static/client.html",
              "~/Projects/sandbox/blog.html", "~/Desktop/contact.html"]
GOALS = ["把整个仓库迁移完", "三个月内找到大模型应用开发的工作", "把评测集扩到 300 条",
         "每周写一篇技术笔记", "把 RAG 检索延迟压到 200ms 以内", "学完 MLX 微调",
         "把界面做到手机可用", "攒够 1000 条训练数据"]
WRITE_TARGETS = ["~/Projects/sandbox/test.md", "~/Desktop/笔记.txt", "~/Projects/sandbox/out.json",
                 "~/Desktop/总结.md", "data/note.md", "~/Projects/sandbox/tmp.txt",
                 "~/Desktop/todo.md", "~/Projects/model-studio/data/draft.md"]
EDIT_TARGETS = ["~/Projects/sandbox/fib.py", "app/router.py", "app/tools.py",
                "static/app.html", "~/Projects/sandbox/demo.md", "training/eval_router.py",
                "app/agent.py", "~/Desktop/snippet.py"]


def P(*phrasings):
    return list(phrasings)


# 每个工具若干 (模板id, 工具, 问法, 槽位, 参数构造)
SPECS: list[tuple[str, str, list[str], list[str], callable]] = [
    ("ws_ver", "web_search", P("{x} 现在最新版本是多少", "{x} 最新版是几", "查一下 {x} 的版本号"),
     BRANDS, lambda x: {"query": f"{x} 最新版本"}),
    ("ws_look", "web_search", P("帮我搜一下 {x}", "搜搜 {x}", "{x} 怎么弄", "网上 {x} 一般怎么做"),
     ["树状数组的模板题", "Mac 上怎么装 ffmpeg", "MLX 支持哪些模型", "vibe coding 是什么",
      "怎样写好 README", "peft 和 lora 区别", "macOS 如何查看端口占用", "Python 打包成 exe"],
     lambda x: {"query": x}),
    ("ws_news", "web_search", P("{x} 最近有什么新闻", "看看 {x} 的最新动态", "{x} 有什么新消息"),
     ["AI 芯片", "开源大模型", "苹果发布会", "显卡行情", "MLX 框架", "国产 GPU",
      "量化模型", "推理加速"],
     lambda x: {"query": f"{x} 新闻"}),
    ("ws_err", "web_search", P("{x} 报错怎么解决", "遇到 {x} 怎么处理", "{x} 是什么原因"),
     ["ModuleNotFoundError: mlx", "CUDA out of memory", "permission denied",
      "Segmentation fault", "zsh: command not found", "HTTP 429 Too Many Requests",
      "SSL certificate problem", "docker: no space left"],
     lambda x: {"query": f"{x} 报错"}),
    ("rf_read", "read_file", P("读一下 {x}", "打开 {x} 看看", "{x} 里写了什么", "看一下 {x} 的内容"),
     FILES, lambda x: {"path": x}),
    ("rf_show", "read_file", P("把 {x} 的内容给我看看", "念一下 {x}", "{x} 讲了啥"),
     FILES[:8], lambda x: {"path": x}),
    ("wf_new", "write_file", P("新建一个 {x}，内容是 hello", "在 {x} 写个 hello", "创建 {x} 并写入 hello"),
     WRITE_TARGETS, lambda x: {"path": x, "content": "hello"}),
    ("wf_save", "write_file", P("把这些内容保存到 {x}", "存成 {x}", "{x} 里存一份"),
     WRITE_TARGETS, lambda x: {"path": x, "content": "..."}),
    ("ef_fix", "edit_file", P("把 {x} 里的 foo 改成 bar", "{x} 里 foo 换成 bar", "改 {x}：foo → bar"),
     EDIT_TARGETS, lambda x: {"path": x, "old_string": "foo", "new_string": "bar"}),
    ("ld_ls", "list_dir", P("看一下 {x} 目录里有什么", "{x} 下有哪些文件", "列一下 {x}"),
     DIRS, lambda x: {"path": x}),
    ("ff_find", "find_files", P("找出所有 {x} 文件", "找一下 {x} 后缀的文件", "哪里有 {x} 文件"),
     EXTS, lambda x: {"pattern": f"*.{x}"}),
    ("gc_grep", "grep_code", P("代码里哪里用了 {x}", "搜哪些文件提到 {x}", "全局搜 {x}"),
     PATTERNS, lambda x: {"pattern": x}),
    ("rb_cmd", "run_bash", P("跑一下 {x}", "执行 {x}", "在终端执行 {x}"),
     CMDS, lambda x: {"command": x}),
    ("rb_time", "run_bash", P("执行 {x} 看看输出", "跑 {x} 结果贴给我", "{x} 的输出是什么"),
     CMDS, lambda x: {"command": x}),
    ("fu_get", "fetch_url", P("抓一下 {x} 的正文", "把 {x} 抓下来", "取 {x} 的内容"),
     URLS, lambda x: {"url": x}),
    ("fu_read", "fetch_url", P("把 {x} 这页内容读出来", "{x} 这个链接讲了什么", "看看 {x} 说了啥"),
     URLS, lambda x: {"url": x}),
    ("ks_algo", "knowledge_search", P("{x} 的原理是什么", "{x} 怎么理解", "讲讲 {x}"),
     QUERIES, lambda x: {"query": f"{x} 原理"}),
    ("ks_q", "knowledge_search", P("查一下知识库里的 {x}", "本地资料里有没有 {x}", "知识库搜 {x}"),
     QUERIES, lambda x: {"query": x}),
    ("w_city", "get_weather", P("{x} 今天天气怎么样", "{x} 冷不冷", "{x} 要带伞吗", "{x} 现在多少度"),
     CITIES, lambda x: {"city": x}),
    ("wx_rate", "get_exchange_rate", P("{x} 换人民币多少钱", "{x} 现在汇率多少", "查一下 {x} 汇率"),
     CURRENCIES, lambda x: {"base": x, "targets": "CNY"}),
    ("gi_img", "generate_image", P("画一张 {x} 的图", "生成一张 {x}", "给我张 {x} 的插画"),
     IMG_SUBJ, lambda x: {"prompt": x}),
    ("mc_chart", "make_chart", P("把 {x} 的数据画成柱状图", "{x} 用图表示", "画个 {x} 的折线图"),
     CHART_SUBJ, lambda x: {"spec": json.dumps(
         {"type": "bar", "title": x, "series": [{"name": "值", "y": [1, 2, 3]}]},
         ensure_ascii=False)}),
    ("ad_csv", "analyze_data", P("分析一下 {x} 这份数据", "{x} 里有多少行", "看看 {x} 的结构"),
     DATA_FILES, lambda x: {"path": x}),
    ("of_md", "office", P("把 {x} 转成 docx", "{x} 导出成 Word", "把 {x} 处理成 docx 文件"),
     MD_FILES, lambda x: {"action": "md2docx", "path": x}),
    ("of_xlsx", "office", P("读一下 {x} 这个表格", "{x} 里的数据念一下", "打开 {x} 看内容"),
     ["~/Desktop/报表.xlsx", "~/Projects/sandbox/data.xlsx", "~/Desktop/成绩.xlsx",
      "data/月度统计.xlsx", "~/Desktop/考勤.xlsx", "~/Projects/sandbox/库存.xlsx",
      "~/Desktop/预算.xlsx", "data/prices.xlsx"],
     lambda x: {"action": "read_xlsx", "path": x}),
    ("dg_check", "design", P("检查 {x} 的布局有没有问题", "{x} 排版对不对", "{x} 有没有溢出"),
     HTML_FILES, lambda x: {"action": "check", "path": x}),
    ("sg_goal", "set_goal", P("记一下我的目标：{x}", "把「{x}」设成目标", "添加目标 {x}"),
     GOALS, lambda x: {"goal": x}),
]


# --------------------------------------------------------------------- 真实样本
SKIP_TOOLS = {"重启模型", "压缩上下文", "write_plan", "spawn_agents"}


def load_real() -> list[tuple[str, dict]]:
    out, seen = [], set()
    for f in sorted(SESSIONS.glob("*.json")):
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        goal = (s.get("goal") or "").strip()
        if len(goal) < 2:
            continue
        for st in (s.get("steps") or []):
            name = st.get("tool")
            if name in SKIP_TOOLS or name not in TOOL_NAMES:
                continue
            args = st.get("args") or {}
            if not isinstance(args, dict) or not args:
                continue
            key = (goal[:60], name)
            if key in seen:
                continue
            seen.add(key)
            out.append((goal[:300], {"tool": name, "args": args}))
            break
    return out


def load_synth() -> list[tuple[str, dict, str]]:
    """(问题, 目标, 模板id)。"""
    out = []
    for tid, tool, phrasings, slots, build in SPECS:
        for slot in slots:
            for tpl in phrasings:
                out.append((tpl.format(x=slot), {"tool": tool, "args": build(slot)}, tid))
    return out


def build_ood() -> list[tuple[str, dict]]:
    """OOD 测试集：每个工具**整组留出一种问法**，训练里完全见不到。

    为什么还需要它：分层切分只是换了槽位、问法没变，
    测出来 97% 只能说明"背下了句式"。真正要回答的是
    "换个说法还认不认得" —— 那必须留出训练里没有的句式。
    """
    # 每个工具挑一个模板整组留出（优先挑问法最多的那个，样本更足）
    by_tool: dict[str, list[tuple[str, list[str], list[str]]]] = {}
    for tid, tool, phrasings, slots, _b in SPECS:
        by_tool.setdefault(tool, []).append((tid, phrasings, slots))

    ood, keep_tids = [], set()
    for tool, group in by_tool.items():
        if len(group) >= 2:
            # 多模板工具：留出最后一个模板
            hold = group[-1]
            ood.append((tool, hold[0], hold[2]))
            keep_tids.update(g[0] for g in group[:-1])
        else:
            # 单模板工具：OOD 不可能，训练里保留
            ood_none = group[0]
            keep_tids.add(ood_none[0])

    build_map = {tid: (tool, build) for tid, tool, _p, _s, build in SPECS}
    out = []
    for tool, tid, slots in ood:
        _tool, build = build_map[tid]
        phrasings = next(p for t, _tl, p, _s, _b in SPECS if t == tid)
        for slot in slots:
            for tpl in phrasings:
                out.append((tpl.format(x=slot), {"tool": tool, "args": build(slot)}))
    return out, keep_tids


# --------------------------------------------------------------------- 分层切分
def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    real = load_real()
    synth_all = load_synth()
    ood_pairs, keep_tids = build_ood()

    # 训练/验证/测试只用在留出范围之外的模板
    synth = [(q, t) for q, t, tid in synth_all if tid in keep_tids]

    # 按工具分组
    by_tool: dict[str, list[tuple[str, dict, str]]] = {}
    for q, t in synth:
        by_tool.setdefault(t["tool"], []).append((q, t, "synth"))
    for q, t in real:
        by_tool.setdefault(t["tool"], []).append((q, t, "real"))

    train, valid, test, meta, per_tool = [], [], [], [], {}
    for tool, items in sorted(by_tool.items()):
        # 去重（同工具内问题文本唯一）
        seen, uniq = set(), []
        for q, t, src in items:
            k = re.sub(r"\s+", "", q)[:80]
            if k in seen:
                continue
            seen.add(k)
            uniq.append((q, t, src))
        random.shuffle(uniq)

        n = len(uniq)
        n_test = max(2, round(n * 0.1))
        n_valid = max(1, round(n * 0.1))
        # 保证训练侧至少 60%
        if n_test + n_valid > n * 0.4:
            n_test, n_valid = max(2, round(n * 0.08)), max(1, round(n * 0.08))

        te = uniq[:n_test]
        va = uniq[n_test:n_test + n_valid]
        tr = uniq[n_test + n_valid:]
        per_tool[tool] = {"total": n, "train": len(tr), "valid": len(va), "test": len(te)}

        for q, t, _src in tr:
            train.append(_msg(q, t["tool"], t["args"]))
        for q, t, _src in va:
            valid.append(_msg(q, t["tool"], t["args"]))
        for q, t, src in te:
            test.append(_msg(q, t["tool"], t["args"]))
            meta.append({"tool": t["tool"], "src": src})

    # 打乱（同工具内已洗过；整体再洗一次避免按工具聚集）
    random.shuffle(train)
    random.shuffle(test)

    def write(name, items):
        p = OUT / name
        with p.open("w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        return len(items)

    n_tr, n_va, n_te = write("train.jsonl", train), write("valid.jsonl", valid), write("test.jsonl", test)
    with (OUT / "test_meta.jsonl").open("w", encoding="utf-8") as f:
        for m in meta:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")

    # OOD 集：问法没见过的测试集（真正的泛化考核）
    ood_msgs = [_msg(q, t["tool"], t["args"]) for q, t in ood_pairs]
    n_ood = write("test_ood.jsonl", ood_msgs)
    with (OUT / "test_ood_meta.jsonl").open("w", encoding="utf-8") as f:
        for q, t in ood_pairs:
            f.write(json.dumps({"tool": t["tool"], "src": "ood"}, ensure_ascii=False) + "\n")

    # 校验：每条目标合法 + 每个工具训练侧条数够
    bad, thin = 0, []
    for p in ("train.jsonl", "valid.jsonl", "test.jsonl"):
        for line in (OUT / p).read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            try:
                obj = json.loads(d["messages"][-1]["content"])
                assert obj["tool"] in TOOL_NAMES
            except Exception:                       # noqa: BLE001
                bad += 1
    for tool, c in per_tool.items():
        if c["train"] < 20:
            thin.append((tool, c["train"]))
    if bad:
        raise SystemExit(f"[失败] {bad} 条目标非法")
    if thin:
        print(f"[警告] 这些工具训练样本 <20：{thin}")

    (OUT / "stats.json").write_text(json.dumps(
        {"seed": SEED, "train": n_tr, "valid": n_va, "test": n_te,
         "real": len(real), "synth": len(synth), "per_tool": per_tool},
        ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"真实 {len(real)} 条 / 合成（训练池）{len(synth)} 条")
    print(f"写出 train {n_tr} / valid {n_va} / test {n_te} / OOD {n_ood}")
    print(f"{'工具':<20}{'总':>5}{'train':>7}{'test':>6}")
    for tool, c in sorted(per_tool.items(), key=lambda kv: -kv[1]["total"]):
        print(f"{tool:<20}{c['total']:>5}{c['train']:>7}{c['test']:>6}")
    print("格式校验通过")


if __name__ == "__main__":
    main()
