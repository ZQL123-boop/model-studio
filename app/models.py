"""本地模型发现。

扫描约定目录，识别 MLX / GGUF / Ollama 三类，给出可用的启动方式。

判据只用文件特征，不猜：
- GGUF：目录里有 *.gguf
- MLX：有 config.json 且 model_type 字段存在，且有 *.safetensors
- Ollama：~/.ollama/models/manifests 下有 manifest（当前机器为空，代码留着）

MLX 模型需要一个装了 mlx-lm 的 Python 解释器。这里向上找 `.venv`：
`~/Projects/qwen-local/models/xxx` → 用 `~/Projects/qwen-local/.venv/bin/python`。
找不到就退回当前解释器。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from functools import lru_cache
from dataclasses import dataclass, asdict, field
from pathlib import Path

HOME = Path.home()
# 扫描根：这些目录下的一级或二级子目录都可能是模型
SCAN_ROOTS = [
    HOME / "Projects",
    HOME / "models",
    HOME / ".cache/huggingface/hub",
]


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


@lru_cache(maxsize=64)
def _find_mlx_python(model_dir: Path) -> str | None:
    """向上找 venv，确认里面有 mlx_lm。

    **不用 import 验证**：`import mlx_lm` 要 3~5 秒（要加载 MLX 和 Metal），
    扫描 1 个模型就要 5 秒、3 个模型就 15 秒，/v1/models 会被 agent 判超时。
    查 site-packages 里有没有 mlx_lm 目录，毫秒级，效果一样。
    """
    for parent in [model_dir, *model_dir.parents[:4]]:
        venv = parent / ".venv"
        py = venv / "bin" / "python"
        if not py.exists():
            continue
        if any(venv.glob("lib/python*/site-packages/mlx_lm")):
            return str(py)
    # 回退：模型目录旁边没有 venv（例如 qwen38-local 和 30B 共用解释器）
    fallback = HOME / "Projects/qwen-local/.venv"
    py = fallback / "bin" / "python"
    if py.exists() and any(fallback.glob("lib/python*/site-packages/mlx_lm")):
        return str(py)
    return None


@dataclass
class ModelSpec:
    id: str
    name: str
    path: str
    kind: str                 # mlx | gguf | ollama
    size_gb: float
    ready: bool               # 能否启动
    note: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# 只认因果语言模型。bge-m3 / bge-reranker 这类 embedding、reranker 也是
# config.json + safetensors 的结构，但 mlx_lm 加载不了，必须排除。
_LLAMA_CANDIDATES = [
    Path.home() / ".local/bin/llama-server",
    Path.home() / "Projects/qwen38-local/bin/llama-server",
    Path.home() / "Projects/qwen38-local/llama-b11963/llama-server",
    Path("/opt/homebrew/bin/llama-server"),
    Path("/usr/local/bin/llama-server"),
]


def find_llama() -> str | None:
    """找 llama-server 可执行文件。

    不能只靠 shutil.which：服务是 `bash run.sh` 起的，PATH 继承自 launchd
    （/usr/bin:/bin:/usr/sbin:/sbin），而 llama-server 装在 ~/.local/bin。
    结果就是命令行里能跑、服务里找不到，界面把 GGUF 标成"不可用"。
    """
    w = shutil.which("llama-server")
    if w:
        return w
    for p in _LLAMA_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


_CAUSAL_TYPES = {
    "qwen2", "qwen2_moe", "qwen3", "qwen3_moe", "llama", "mistral",
    "gemma", "gemma2", "gemma3", "phi3", "deepseek_v2", "deepseek_v3",
    "glm4", "internlm2", "baichuan", "minicpm", "yi", "falcon", "gpt2",
    "qwen3_5", "qwen3_5_moe", "qwen3_next",
}


def _is_causal_lm(cfg: dict) -> bool:
    """能生成文本的模型。

    ForCausalLM 之外还要认 ForConditionalGeneration——Qwen3.8 这类视觉语言模型
    用的是 `Qwen3_5ForConditionalGeneration`，只认 CausalLM 会把它们全漏掉。
    """
    archs = cfg.get("architectures") or []
    if any(("ForCausalLM" in a) or ("ForConditionalGeneration" in a) for a in archs):
        return True
    return (cfg.get("model_type") or "") in _CAUSAL_TYPES


def _is_model_dir(p: Path) -> bool:
    if not p.is_dir():
        return False
    # 训练用基座（0.5B/1.7B 这种）不该出现在对话模型列表里：
    # 它们不会调工具，选错会直接输出幻觉 JSON（实测踩过）。
    # 打一个 .train_only 标记即可排除，训练脚本按路径直接加载，不受影响。
    if (p / ".train_only").exists():
        return False
    names = {f.name for f in p.iterdir() if f.is_file()}
    if any(n.endswith(".gguf") for n in names):
        return True
    if "config.json" in names and any(n.endswith(".safetensors") for n in names):
        return True
    return False


def _spec_from_gguf(p: Path) -> ModelSpec | None:
    """单个 .gguf 文件（不是目录）也能当一个模型。

    Unsloth 的量化仓库经常是一个目录里放多个 .gguf，下载下来是散文件；
    以前只扫目录，这类模型永远扫不到。
    """
    if not p.is_file() or p.suffix.lower() != ".gguf":
        return None
    llama = find_llama()
    rel = str(p.relative_to(HOME)).replace("/", "__")
    return ModelSpec(
        id=rel, name=p.stem, path=str(p), kind="gguf",
        size_gb=round(p.stat().st_size / 1024**3, 2), ready=bool(llama),
        note="" if llama else "未找到 llama-server（不在 PATH）",
        meta={"file": p.name},
    )


def _spec_from_dir(p: Path) -> ModelSpec | None:
    if not _is_model_dir(p):
        return None
    files = list(p.iterdir())
    names = {f.name for f in files}
    rel = str(p.relative_to(HOME))
    mid = rel.replace("/", "__")

    if any(n.endswith(".gguf") for n in names):
        llama = find_llama()
        size = sum(f.stat().st_size for f in files if f.suffix == ".gguf")
        return ModelSpec(
            id=mid, name=p.name, path=str(p), kind="gguf",
            size_gb=round(size / 1024**3, 2), ready=bool(llama),
            note="" if llama else "未安装 llama.cpp（llama-server 不在 PATH）",
            meta={"files": sorted(n for n in names if n.endswith(".gguf"))},
        )

    try:
        cfg = json.loads((p / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not _is_causal_lm(cfg):
        return None      # embedding / reranker / 分类模型，不归本工具管

    py = _find_mlx_python(p)
    quant = cfg.get("quantization") or {}
    bits = quant.get("bits")
    size = _dir_size(p)
    n_experts = cfg.get("num_experts")
    note = ""
    if not py:
        note = "找不到装了 mlx-lm 的 .venv"
    elif bits and bits <= 4 and size > 14 * 1024**3 and n_experts:
        note = f"{bits}bit / {n_experts} 专家，24GB 内存下余量紧张"

    return ModelSpec(
        id=mid, name=p.name, path=str(p), kind="mlx",
        size_gb=round(size / 1024**3, 2), ready=bool(py), note=note,
        meta={
            "python": py,
            "model_type": cfg.get("model_type"),
            "layers": cfg.get("num_hidden_layers"),
            "experts": n_experts,
            "experts_per_tok": cfg.get("num_experts_per_tok"),
            "bits": bits,
        },
    )


def _scan_ollama() -> list[ModelSpec]:
    """Ollama 的模型在 manifests 里，只有名字没有权重路径。当前机器为空。"""
    root = HOME / ".ollama" / "models" / "manifests"
    out: list[ModelSpec] = []
    if not root.exists():
        return out
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        name = f"{f.parent.name}:{f.name}"
        # manifest 里 layers 的 size 之和才是真实占用。写死 0.0 会让人以为没占空间。
        size = 0
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
            size = sum(int(layer.get("size") or 0) for layer in m.get("layers", []))
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        out.append(ModelSpec(
            id=f"ollama__{name}", name=name, path=str(f.parent),
            kind="ollama", size_gb=round(size / 1024**3, 2), ready=True,
            note="由 Ollama 自己管理，不需要 Model Studio 启停", meta={},
        ))
    return out


# 整个扫描结果缓存 60 秒。不加缓存的话 /v1/models 要 12 秒——
# 每个模型都要起一个 python 子进程验证 "import mlx_lm" 能用，
# agent 调用方等不起。
_CACHE: tuple[float, list[ModelSpec]] | None = None
_CACHE_TTL = 60.0


def discover(force: bool = False) -> list[ModelSpec]:
    global _CACHE
    if not force and _CACHE and time.time() - _CACHE[0] < _CACHE_TTL:
        return _CACHE[1]

    found: dict[str, ModelSpec] = {}

    # 三个深度都扫：<root>/<model>、<root>/models/<model>、<root>/*/models/<model>
    for root in SCAN_ROOTS[:2]:
        if not root.exists():
            continue
        cands = [root, root / "models", *root.glob("*/models")]
        for cand in cands:
            if not cand.is_dir():
                continue
            for child in sorted(cand.iterdir()):
                spec = _spec_from_gguf(child) if child.is_file() else _spec_from_dir(child)
                if spec:
                    found[spec.id] = spec

    # HF hub 结构：models--org--name/snapshots/<hash>/
    hf = SCAN_ROOTS[2]
    if hf.exists():
        for repo in hf.iterdir():
            snap = repo / "snapshots"
            if not snap.is_dir():
                continue
            for rev in snap.iterdir():
                spec = _spec_from_dir(rev)
                if spec:
                    spec.name = repo.name.replace("models--", "").replace("--", "/")
                    found[spec.id] = spec

    for spec in _scan_ollama():
        found[spec.id] = spec

    out = sorted(found.values(), key=lambda s: -s.size_gb)
    _CACHE = (time.time(), out)
    return out


def find(model_id: str) -> ModelSpec | None:
    for s in discover():
        if s.id == model_id:
            return s
    return None


if __name__ == "__main__":
    for m in discover():
        print(f"{m.name:<45} {m.kind:<7} {m.size_gb:>6.1f} GB  "
              f"{'就绪' if m.ready else '不可用'}  {m.note}")
