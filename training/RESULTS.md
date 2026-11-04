# 训练结果 v3（2026-11-01 21:30）—— 这一版能用了

## 一句话

**0.5B 分类器 + 30B 生成的口语改写 + 规则填参数**：换说法测试从 31.4% 干到 **83.1%**。
关键是**数据里的句式多样性**，不是换模型、不是调超参。

## 三版对比（同一个 OOD 测试集，207 条，问法训练里没见过）

| 版本 | 做法 | 同句式 test | **OOD 工具准确** |
|---|---|---|---|
| v1 | 0.5B 输出 JSON（含参数） | 97.1% | 60.4% |
| v2 | 0.5B 只选工具（18 分类），无改写数据 | 97.1% | 48.8% |
| **v3** | **0.5B 分类 + 324 条 30B 改写** | 94.2% | **83.1%** |
| — | 1.7B 输出 JSON（对照） | 95.7% | 66.2% |

**v2 → v3 只改了一件事**：训练集里加入 324 条由本地 30B 生成的口语化改写
（同一意图换 6 种说法）。OOD 从 48.8% → 83.1%，**+34 pt**。

## 为什么之前一直卡在 60%

合成数据是模板拼的，每个意图只有 3~4 种句式。模型学到的是
「句式 → 工具」的查表，换个说法就废。
增大模型（0.5B→1.7B）只把 61.4% 抬到 66.2%；换输出格式（JSON→分类）反而更差（48.8%）。
**只有加句式多样性有效。**

## 分工设计（这版的核心）

```
用户一句话
  ├─ 0.5B 分类器（LoRA，输出 3~10 token）→ 工具名，18 选 1
  ├─ 规则抽取 → 路径 / URL / 命令 / 城市 / 币种 / office action
  └─ 规则抽不到（约 8.7%）→ 交 30B 补参数
```

参数为什么不交给小模型：v1 实测 `web_search` 选对 100% 但 query 只对 4.2%，
`knowledge_search` 选对 40% 但 query 0% —— **会选不会填**。
而路径、URL、命令、城市这些在文本里是**显式出现**的，正则比模型稳。

规则覆盖率（在 OOD 207 条上，假设工具选对）：

| 工具 | 规则能独立给出参数 |
|---|---|
| web_search / read_file / write_file / fetch_url / knowledge_search | 100% |
| office | 88% |
| run_bash | 67%（"看看系统跑了多久"这种没有命令文本） |
| **合计** | **189/207 = 91.3%**，其余 18 条需 30B |

## 分工具 OOD 表现（v3）

| 工具 | 准确 | 说明 |
|---|---|---|
| read_file / web_search | 100% | 稳 |
| knowledge_search / run_bash | 98% | 稳 |
| write_file | 79% | 与 edit_file 偶有混淆 |
| fetch_url | 46% | 和 read_file / web_search 边界模糊 |
| office | 38% | action 语义太细（md2docx vs read_xlsx） |

**剩下两个短板是边界定义问题**，不是模型能力问题：
`fetch_url`（给 URL 抓正文）与 `read_file`（读本地）对模型来说都是"读内容"；
`office` 一个工具装了 5 个 action，模型要先选工具再猜 action。

## 数据

| 项 | 条数 |
|---|---|
| 模板合成 | 671 |
| 真实（agent_sessions） | 34 |
| **30B 生成的口语改写** | **324** |
| train / valid / test / OOD | 822 / 103 / 103 / 207 |

改写生成：`augment_paraphrase.py`，54 个种子 × 6 种说法，17 秒/种子，约 17 分钟。
只让 30B 换措辞、不让它定工具——标签继承原意图，避免引入标注错误。

## 训练配置（v3）

| 项 | 值 |
|---|---|
| 基座 | Qwen2.5-0.5B-Instruct-4bit |
| LoRA | 16 层 / rank 8 / 2.93M 可训练（0.594%） |
| 超参 | batch 8 / lr 1e-4 / 350 iters / max_seq 512 / mask-prompt / seed 20261101 |
| 耗时 | 约 12 分钟（M2 24 GB） |
| 最终 train loss | 0.003 |

## 怎么用

```bash
# 推理（必须用装了 mlx 的 venv）
~/Projects/qwen-local/.venv/bin/python training/router.py "把 ~/Desktop/笔记.md 转成 docx"
# → office  args={'action':'md2docx','path':'~/Desktop/笔记.md'}  需30B=False
```

代码里：

```python
from router import Router
r = Router()                       # 默认 models/qwen2.5-0.5b-4bit + adapters_cls2
out = r.route("帮我看看深圳今天冷不冷")
# {'tool': 'get_weather', 'args': {'city': '深圳'}, 'need_llm_args': False, ...}
```

## 下一步（还能涨的点）

1. **拆 office**：`md2docx` / `read_xlsx` / `make_pptx` 变成独立工具名，38% 能到 80%+
2. **划清 fetch_url / read_file 边界**：在工具描述里写明"必须是 http(s) 链接才用 fetch_url"
3. **继续加改写**：目标每工具 100+ 条口语样本（现在最多 125、最少 20）
4. **接进 agent 循环**：先用它选工具 + 填参数，30B 只负责生成/执行；省 token 又少绕圈

## 复现

```bash
PY=~/Projects/qwen-local/.venv/bin/python
PY training/build_dataset.py            # JSON 版（v1 对比用）
PY training/augment_paraphrase.py       # 30B 改写（约 17 分钟）
PY training/build_cls_dataset.py        # 分类版数据集（并入改写）
$LORA --model models/qwen2.5-0.5b-4bit --train --data training/data_cls ...
PY training/eval_cls.py --adapter training/adapters_cls2            # test
PY training/eval_cls.py --adapter training/adapters_cls2 --split ood  # OOD
```


---

# 补充：1.7B 也训了（2026-11-01 23:40）

同一份分类数据（train 822），换成 1.7B 基座：

| 模型 | 零样本 test | 零样本 OOD | 训练后 test | **训练后 OOD** |
|---|---|---|---|---|
| 0.5B | 28.6% | — | 94.2% | 83.1% |
| **1.7B** | 66.0% | 51.7% | 92.2% | **91.8%** |

**结论：值得训的是 1.7B。** OOD 91.8% vs 0.5B 83.1%（+8.7 pt），
而且 `office` 从 38% 直接干到 **100%**、`fetch_url` 46% → 79%
（唯一退步：`knowledge_search` 98% → 71%）。

代价：
| | 0.5B | 1.7B |
|---|---|---|
| 训练耗时 | 12 分钟 | **30 分钟** |
| 模型体积 | 289 MB | 1.3 GB |
| 单次推理 | 最快 | 约 3 倍耗时 |
| OOD 准确 | 83.1% | **91.8%** |

选型建议：**要精度用 1.7B，要延迟/省内存用 0.5B**（83% 也已经可用）。

---

# 第四轮：数据清洗 + 扩量（2026-11-02 01:10）

## 做了什么

1. **清洗泄漏**：发现早期改写数据里有 7 条与测试集近似重复（2.9%），已删除
2. **补弱项**：针对 1.7B 的两个短板（knowledge_search 71%、fetch_url 79%）追加 95 条改写
3. 改写数据**只进 train**（此前会混进 test，抬高分数）
4. 数据集：train 1127 / test 70 / OOD 207

## 结果（train 1127，只训 300 步）

| 指标 | 旧（822 train / 300 步） | 新（1127 train / 300 步） |
|---|---|---|
| test | 92.2% | **98.6%** |
| OOD | 91.8% | **72.9%** ↓ |
| office | 100% | **0%** |
| web_search | 100% | 42% |

**这是"训练不足"，不是"数据变差"**：1127 条 / batch 4 = 282 步/epoch，
300 步只有 1.06 个 epoch（旧配置是 1.46 个）。模型没学完就停了。
`office` 掉到 0% 是最明显的信号——这类细粒度工具最需要足够的更新次数。

## 修复

已按 520 步（≈1.8 epoch）重跑，产物 `training/adapters_cls17c`，约 55 分钟。

**教训：改数据量必须同步改 iters**。经验公式：

```
iters ≈ (样本数 / batch) × 目标 epoch 数
1127 / 4 × 1.8 ≈ 500
```

---

# 第五轮：清洗后的真实成绩（2026-11-02 01:55）—— 之前的 91.8% 是虚高的

## 先认错

上一轮的 **1.7B OOD 91.8% 是被污染的**：那批改写数据的种子取自 OOD 留出模板，
等于模型提前见过考题的句式家族。清洗后同配置掉到 **72.0%**。

**能看出问题的证据**：`office` 从 100% 变 0%。查错例发现：

```
Q: 读一下 ~/Desktop/报表.xlsx 这个表格
   → read_file   （应为 office）
```

训练集里 office 只有"转成 docx"一种动作，OOD 考的是"读 xlsx"。
模型没见过这个动作，于是猜了 read_file —— **这不是模型变笨，是测试设计有洞**：
`office` 是"一个工具装多个 action"，整组留出一个模板等于把这个 action 整个拿走，
模型不可能学会。

## 干净数据下的真实对比（同一份 train 1127 / OOD 207）

| 模型 | 训练配置 | test | **OOD** | 剔除 office 后 |
|---|---|---|---|---|
| **0.5B** | batch 8 / 350 iters（≈2.5 epoch） | 98.6% | **78.7%** | **89.1%** |
| 1.7B | batch 4 / 520 iters（≈1.8 epoch） | 98.6% | 72.0% | 81.4% |

**0.5B 反而更好**，而且训练只花 12 分钟（1.7B 要 55 分钟）、模型 289 MB（1.7B 1.3 GB）。

原因不复杂：这是**18 选 1 的窄任务**，0.5B 容量够；而它每个样本被更新的次数更多
（2.5 epoch vs 1.8 epoch）。1.7B 在这轮里其实是"训得不够"，不是"能力不够"。

分工具（0.5B）：read_file 100%、fetch_url 92%、web_search 88%、
knowledge_search 83%、run_bash 82%、**office 0%（测试设计缺陷）**。

## 要修的两件事

1. **office 的 OOD 设计**：它是多 action 工具，应该按 action 层面切分，
   或给它补第三个模板（md2docx / read_xlsx / make_pptx），留一个还有两个可学。
2. **1.7B 要公平比较**：得让它跑到 2.5 epoch（约 700 iters / 40 分钟），
   否则现在这个对比对 1.7B 不公平。

## 结论（可用的）

- 现在能用的最好模型：**0.5B + adapters_cls05c，OOD 78.7%（不含 office 缺陷项 89.1%）**
- 数据管线已修干净：562 条改写只进 train、与测试集近似重复已归零
- 91.8% 那个数字作废，别再引用
