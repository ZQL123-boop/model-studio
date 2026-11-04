# 当前模型能不能训？—— 逐个体检（2026-11-01 22:55 实测）

## 结论

| 模型 | 类型 | 体积 | 能 LoRA 训吗 | 内存估算 | 依据 |
|---|---|---|---|---|---|
| **Qwen2.5-0.5B-Instruct-4bit** | 稠密 24 层 | 289 MB | ✅ **能** | < 2 GB | **实测训过 3 次**，350 iters / 12 分钟 |
| **Qwen3-1.7B-4bit** | 稠密 28 层 | 1.3 GB | ✅ **能** | 4~5 GB | **实测训过**，250 iters / 22 分钟 |
| **Qwen3-30B-A3B-4bit** | **MoE 128 专家 / 48 层** | 16 GB | ⚠️ 理论可以，**这台机器不行** | 16 GB + 5 GB | 按参数量推算，**未实跑**（见下方算账） |

机器：M2 / 24 GB（当前系统已占 8.6 GB，空闲约 10~15 GB）。

> 0.5B / 1.7B 两行是**今天实测**（1.7B 刚用 3 iter 探针复验：适配器 4.98M 参数，正常出 loss，无 OOM）。
> 30B 一行是**按参数量和内存现状推算**，没敢实跑——加载 16 GB 权重在只剩 10~15 GB 的机器上
> 有很大概率触发疯狂 swap、把整机拖死。要试的话建议先关掉所有其他应用。

---

## 30B 为什么不行：算一下就知道

MLX 的 LoRA 是**支持 MoE 专家层的**（`SwitchLinear / QuantizedSwitchLinear` 会转成 `LoRASwitchLinear`），
所以不是"框架不支持"，是**内存装不下**：

| 项 | 参数 | 显存/内存占用 |
|---|---|---|
| 基座权重（4bit，冻结） | — | **16 GB** |
| 专家层 LoRA（128 专家 × 48 层 × 3 矩阵，r=8） | ≈ 4.15 亿 | 1.7 GB（fp32）+ Adam 状态 3.3 GB ≈ **5 GB** |
| 激活 + 梯度（batch 1 / seq 512） | — | 1~2 GB |
| **合计** | | **≈ 22~23 GB** |

而系统本身要 8.6 GB，实际可用 10~15 GB。**差 8 GB 以上，必爆。**

**如果只训注意力层**（用 lora 配置的 `keys` 排除专家层）：适配器只有 ~490 万参数（约 20 MB），
总占用降到 ~17 GB —— 仍在边界，得关掉所有其他应用、把 seq 压到 256 试。
**性价比太低：30B 该干的是"生成训练数据"，不是"被训练"。**

---

## 不能训的还有哪些

| 情况 | 能不能 | 原因 |
|---|---|---|
| GGUF（`.gguf`） | ❌ | llama.cpp 没有可用的训练路径；MLX 只认 safetensors |
| 全参微调（任何规模） | ❌ | 光是 0.5B 全参也要 ~6 GB 优化器状态 + 梯度，24 GB 勉强，1.7B 就没了 |
| 4bit 量化的基座 + LoRA | ✅ | 这就是 QLoRA 思路，MLX 原生支持（`--kv-bits` 与它无关，量化在权重里） |
| 8bit / bf16 基座 + LoRA | ⚠️ | 1.7B bf16 要 3.4 GB 权重 + 优化器，能跑但没优势 |

> 注：`qwen-local/models` 目录现在**只剩 30B 一个模型**（文档里提过的 27B / 14B 已经不在了）。
> 可训的两个小模型在 `~/Projects/model-studio/models/`。

---

## 什么数据量配什么模型

| 训练样本（每个工具） | 推荐 | 理由 |
|---|---|---|
| < 20 | **别训** | 只会背题；改提示词 / 加工具描述更有效 |
| 20 ~ 50 | 0.5B，rank 8，1~2 epoch | 我们 v3 的状态（每工具 20~125 条，OOD 83%） |
| 50 ~ 150 | 0.5B 或 1.7B，rank 8~16，2~3 epoch | 1.7B 开始有优势 |
| 150+ | 1.7B 起，考虑 4B | 0.5B 容量会先到顶 |

判断"该不该训"的四条硬门槛（全过才值得动手）：

1. 每个类别 ≥ 20 条训练样本
2. 有**没进过训练**的测试集（同模板 / 换说法各一套）
3. 训练时能停掉常驻大模型（30B 占 17 GB）
4. 能说清"训完在哪个指标上应该涨"——说不清就别训

---

## 自己复查的命令

```bash
# 1) 看模型清单和结构
ls -lh ~/Projects/model-studio/models/ ~/Projects/qwen-local/models/
python3 -c "import json;c=json.load(open('models/qwen3-1.7b-4bit/config.json'));print(c['model_type'],c['num_hidden_layers'],c.get('quantization'))"

# 2) 看当前可用内存（训练前跑）
curl -s http://127.0.0.1:8600/api/status | python3 -c "import json,sys;d=json.load(sys.stdin);print('模型',d['state'],'| 已用',d['sys']['used_gb'],'/',d['sys']['total_gb'],'GB')"

# 3) 先跑 5 个 iter 试水（OOM 会在前 1 分钟就暴露）
~/Projects/qwen-local/.venv/bin/mlx_lm.lora --model models/qwen3-1.7b-4bit \
  --train --data training/data_cls --iters 5 --batch-size 1 --adapter-path /tmp/lora_probe
rm -rf /tmp/lora_probe
```

---

## 一句话

**0.5B 和 1.7B 都能训（都实测过）；30B 不能训，它是数据生成器，不是被训对象。**
真要提精度，路是"更多真实数据 → 1.7B → 拆细工具边界"，不是"上更大的模型硬训"。
