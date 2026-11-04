"""一键清理内存：停掉模型进程 + 杀掉残留的 mlx_lm.server 孤儿，报告释放量。

macOS 没有 Windows 那种"内存清理"；统一内存里真正占大头的是模型进程。
`purge` 清文件缓存要 root（会弹 sudo 密码），违背"一键"，这里不做。
"""
from __future__ import annotations

import time

import psutil

from .runner import runner


def _mlx_procs() -> list[dict]:
    out = []
    for p in psutil.process_iter(["pid", "cmdline", "memory_info"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "mlx_lm.server" not in cmd:
            continue
        try:
            rss = p.info["memory_info"].rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            rss = 0
        out.append({"pid": p.info["pid"], "rss_gb": round(rss / 1024**3, 2)})
    return out


def clean() -> dict:
    before = psutil.virtual_memory().available
    killed: list[dict] = []

    # 1) 停受管模型（runner 单例，释放 16GB 权重）
    try:
        runner.stop(timeout=10)
    except Exception:                     # noqa: BLE001
        pass

    # 2) 杀残留孤儿（反复 stop/start 留下的、runner 管不到的）
    for p in _mlx_procs():
        try:
            proc = psutil.Process(p["pid"])
            proc.terminate()
            proc.wait(timeout=3)
        except psutil.NoSuchProcess:
            continue
        except psutil.TimeoutExpired:
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
        killed.append(p)

    time.sleep(1.5)          # 等内核回收
    after = psutil.virtual_memory().available
    return {
        "before_gb": round(before / 1024**3, 2),
        "after_gb": round(after / 1024**3, 2),
        "freed_gb": round((after - before) / 1024**3, 2),
        "killed": killed,
        "state": runner.status().get("state"),
    }
