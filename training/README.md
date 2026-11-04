# 训练本地模型的「工具路由器」

目标：训一个**小而快**的模型，学会「这句请求该调哪个工具、参数填什么」。
它不负责回答，只负责路由——把 30B 从“每轮纠结要不要调工具”里解放出来。

## 目录

```
build_dataset.py   生成 train/valid/test + OOD 测试集
eval_router.py     评估：JSON 合法率 / 工具名准确 / 关键参数命中（可按工具拆）
data/              生成的数据（可随时重建）
adapters_v3/       0.5B 的 LoRA 权重（当前最好的一组）
adapters_17b/      1.7B 的 LoRA 权重
```

## 两套测试集，缺一不可

| 测试集 | 怎么造的 | 考什么 |
|---|---|---|
| `test.jsonl` | 同模板、换槽位，按工具分层切 | 记住的句式能不能套对新内容 |
| `test_ood.jsonl` | **每个工具整组留出一种问法**，训练里完全没见过 | 换个说法还认不认得（真泛化） |

只报前者会得到 97% 的假象；`test.jsonl` 97% / `test_ood.jsonl` 60% 才是真实水平。

## 命令

```bash
PY=~/Projects/qwen-local/.venv/bin/python
LORA=~/Projects/qwen-local/.venv/bin/mlx_lm.lora

# 1) 建数据集（固定 seed=20261101，幂等）
$PY training/build_dataset.py

# 2) 基线（不挂 adapter）
$PY training/eval_router.py                 # 同句式
$PY training/eval_router.py --split ood     # 换说法

# 3) 训练 0.5B（8~12 分钟）
$LORA --model models/qwen2.5-0.5b-4bit --train --data training/data \
  --fine-tune-type lora --num-layers 16 --batch-size 4 --iters 300 \
  --learning-rate 1e-4 --max-seq-length 1024 --mask-prompt \
  --steps-per-report 25 --steps-per-eval 100 --save-every 150 \
  --adapter-path training/adapters_v3 --seed 20261101

# 4) 评估
$PY training/eval_router.py --adapter training/adapters_v3
$PY training/eval_router.py --adapter training/adapters_v3 --split ood
```

**训练前必须停掉 30B**（占 17 GB）：`curl -X POST localhost:8600/api/stop -d '{}'`。
训完在界面点「启动」拉回来。

## 数据设计的两条铁律（都踩过）

1. **每个工具的训练样本 ≥20 条**。v1 把 `analyze_data` 整组切进测试集，
   训练里 0 条 → 那 15% 的题必然全错，天花板直接压到 85%。
2. **OOD 必须按模板整组留出，但每个工具还得有别的模板在训练里**。
   否则测的是“能不能猜一个没见过的工具”，不是泛化。

## 已知坑

1. JSON 提取别用正则 `\{.*?\}`：多行嵌套会在内层 `}` 截断，把基线误判成 0 分。
2. 模型可能把 `args` 输出成数组，取值前判类型。
3. HF 下载：`HF_ENDPOINT=https://hf-mirror.com` + `HF_HUB_DISABLE_XET=1`
   （Xet 通道要鉴权，直连 huggingface.co 不通）。
4. 训练输出别接 `| tail`，会缓冲到结束；写文件再 `tail -f`。
5. Qwen3 系列默认输出 `<think>`：评估时 `apply_chat_template(..., enable_thinking=False)`。
6. 单文件大模型（如 1.7B）下载用 `curl -C -` 断点续传，镜像会限速到 ~0.8 MB/s。
