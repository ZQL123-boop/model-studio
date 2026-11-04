"""模型进程的生命周期管理。

同一时刻只允许一个模型运行（24GB 内存跑不了两个），所以这是个单例 runner。

几个刻意的设计：

1. **用 `start_new_session=True` 而不是 nohup。** nohup 只忽略 SIGHUP，
   挡不住终端退出发给整个进程组的信号——在 algo-rag 上踩过这个坑。
2. **stdout 重定向到文件，不用 PIPE。** 没人读的 PIPE 写满 64KB 后子进程会阻塞，
   表现为"服务起了但不响应"。
3. **就绪判定靠 HTTP 探活，不靠固定等待。** mlx_lm.server 打印
   "Starting httpd" 之后还要几秒才真能受理请求。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import psutil

from .models import ModelSpec, find_llama

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"


DEFAULT_PORT = 8080


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def _listen_pid(port: int) -> int | None:
    """找到监听某端口的进程 PID。用 lsof，不依赖 psutil（沙箱里 ps 可能被拦）。"""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=5).stdout
        pids = [int(x) for x in out.split() if x.strip().isdigit()]
        return pids[0] if pids else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class Runner:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.spec: ModelSpec | None = None
        self.port: int = 0
        self.max_tokens: int = 2048   # 服务端 --max-tokens，自动重启要沿用
        self.started_at: float = 0.0
        self.state: str = "stopped"        # stopped | starting | ready | error
        self.error: str = ""
        self._log_path: Path | None = None
        self._log_handle = None
        self._watchdog: threading.Thread | None = None
        self.adopted_pid: int | None = None   # 接管来的进程（studio 重启后遇到）
        self._stopped_at: float = 0.0         # 手动停止的时刻（防接管竞态）

    # ---------------------------------------------------------------- 接管
    def adopt_orphan(self) -> None:
        """接管已经在跑、但不受本进程管理的模型。

        场景：studio 重启后，之前拉起的 mlx_lm.server 还活着（占着 17 GB 和 8080），
        但 runner 是新的、`self.proc` 是 None。不接管的话界面显示"未启动"，
        用户点启动会撞端口，而且这个进程再也停不掉。
        """
        if self.proc is not None or self.state == "ready":
            return
        # 刚手动停过就别急着接管：SIGTERM 之后端口要 1~2 秒才真正释放，
        # 这期间 adopt 会把刚杀掉的进程"复活"，用户看到的是点了停止又变运行中。
        if time.time() - self._stopped_at < 6:
            return
        if not _port_open(DEFAULT_PORT):
            return
        pid = _listen_pid(DEFAULT_PORT)
        if not pid:
            return
        try:
            r = httpx.get(f"http://127.0.0.1:{DEFAULT_PORT}/v1/models", timeout=2)
            if r.status_code != 200:
                return
        except httpx.HTTPError:
            return
        self.adopted_pid = pid
        self.port = DEFAULT_PORT
        self.state = "ready"
        self.started_at = time.time()      # 真实启动时间未知，从现在算
        self.spec = next((m for m in __import__("app.models", fromlist=["models"]).discover()
                          if m.ready), None)

    # ---------------------------------------------------------------- 启动
    def build_cmd(self, spec: ModelSpec, port: int, *, max_tokens: int = 2048,
                  context: int = 32768, thinking: bool = False,
                  extra: list[str] | None = None) -> list[str]:
        if spec.kind == "mlx":
            py = spec.meta.get("python")
            if not py:
                raise RuntimeError("这个模型找不到装了 mlx-lm 的 Python 环境")
            cmd = [py, "-m", "mlx_lm.server",
                   "--model", spec.path,
                   "--host", "127.0.0.1", "--port", str(port),
                   "--max-tokens", str(max_tokens),
                   # mlx_lm.server 默认缓存 **10 个** 不同的 KV cache。
                   # 实测长 system prompt（persona + 20 工具，5011 token）每份
                   # 约 0.49 GB。缓存到 2 份(1.02 GB) 时 Metal 直接 OOM：
                   #   RuntimeError: [METAL] Command buffer execution failed:
                   #   Insufficient Memory (kIOGPUCommandBufferCallbackErrorOutOfMemory)
                   # → generation thread died → 之后所有请求空流/404，且不自愈。
                   # 留 1 份：长提示能命中缓存（省掉每轮 22 秒重算），又不累积。
                   # 0=完全禁用（最稳但慢）；1=单份（默认）；>1 有 OOM 风险。
                   "--prompt-cache-size", os.environ.get("MS_PROMPT_CACHE", "1"),
                   # prefill 默认一次算 2048 token（日志里的 2048/2797）。
                   # 30B 权重 16 GB + 24 GB 统一内存，这一大块 Metal buffer
                   # 就是 OOM 的峰值来源：agent 带上工具结果/历史后必然炸。
                   # 切小块用时间换峰值，实测是长提示能否跑完的分水岭。
                   "--prefill-step-size", os.environ.get("MS_PREFILL_STEP", "512"),
                   # Qwen3 默认输出 <think>，实测 80 token 全是思考、11 秒给不出答案。
                   # 关掉之后 1.2 秒就能答完。
                   "--chat-template-args", json.dumps({"enable_thinking": thinking})]
            # KV cache 8bit 量化（MS_KV_BITS=8）：KV cache 内存减半，代价是
            # 禁用 batching（对 agent 单请求无影响）+ 8bit 量化（几乎无损）。
            # 日志实证：prefill-step-size=512 只扛得住 ~3000 token；用户的长
            # 请求（system+20 工具 schema+历史，实测 3984 token）仍 Metal OOM。
            # 所以默认开 8；要关就设 MS_KV_BITS=0。
            _kv = os.environ.get("MS_KV_BITS", "8").strip()
            if _kv and _kv != "0":
                cmd += ["--kv-bits", _kv]
        elif spec.kind == "gguf":
            # --jinja 不是可选项：Qwen3.8 这类新架构用自定义 chat template，
            # 缺了会输出乱码（社区里"量化模型坏了"的投诉基本都源于此）。
            # KV cache 用 q8_0：32K 上下文只要 1.3 GB，f16 要 2.3 GB。
            cmd = [find_llama() or "llama-server", "-m", spec.path,
                   "--host", "127.0.0.1", "--port", str(port),
                   "--device", "MTL0", "-ngl", "99",
                   "-c", str(context), "--jinja", "-fa", "on",
                   "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
                   # Qwen3.8 的 thinking 默认开启，不关的话 16 个 token 全花在思考上、
                   # 正文一个字都出不来（实测过，返回空字符串）
                   "--reasoning", "off",
                   "-t", "8", "--no-webui"]
        elif spec.kind == "ollama":
            raise RuntimeError("Ollama 模型请通过 ollama serve 启动，本工具暂不支持代管")
        else:
            raise RuntimeError(f"未知模型类型 {spec.kind}")
        return cmd + (extra or [])

    def start(self, spec: ModelSpec, port: int = 8080, **kwargs) -> dict:
        if self.proc and self.proc.poll() is None:
            raise RuntimeError(f"已有模型在运行（{self.spec.name if self.spec else '?'}），先停止")
        if not spec.ready:
            raise RuntimeError(spec.note or "该模型不可启动")

        cmd = self.build_cmd(spec, port, **kwargs)
        # 不能直接 mkdir(exist_ok=True)：这台机器的 sitecustomize shim 在
        # exist_ok=True 时仍会向 broker 申请，broker 发现目录已存在就抛
        # PermissionError: EEXIST。先判 exists 再建，绕开它。
        if not LOG_DIR.exists():
            LOG_DIR.mkdir(parents=True, exist_ok=True)
        safe = spec.name.replace("/", "_")
        self._log_path = LOG_DIR / f"{safe}.log"
        # 追加模式（原来是 "w"）：每次重启清空日志 → 崩现场被销毁，
        # 出现过"重启 5 次但查不到任何原因"的情况。现在保留全部历史，
        # 每次启动打一条带时间戳的分隔线，方便对齐是第几次启动。
        self._log_handle = self._log_path.open("a", encoding="utf-8", buffering=1)
        self._log_handle.write(
            f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"port={port} max_tokens={kwargs.get('max_tokens')} =====\n"
            f"cmd: {' '.join(cmd)}\n")

        env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
        self.proc = subprocess.Popen(
            cmd, cwd=str(Path(spec.path).parent),
            stdout=self._log_handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,          # 脱离会话与进程组
            env=env,
        )
        self.spec, self.port = spec, port
        # 记住本次启动的**服务端**上限。自动重启时不能拿「当前请求的
        # max_tokens」当 --max-tokens：那是单次输出长度，实测会把全局上限
        # 压到 128，之后所有长回答都被截断。
        self.max_tokens = int(kwargs.get("max_tokens") or 2048)
        self.started_at, self.state, self.error = time.time(), "starting", ""

        self._watchdog = threading.Thread(target=self._wait_ready, daemon=True)
        self._watchdog.start()
        return {"state": self.state, "pid": self.proc.pid, "cmd": cmd}

    def _wait_ready(self, timeout: float = 300) -> None:
        """轮询 /v1/models，直到服务真的能受理请求。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is None or self.proc.poll() is not None:
                self.state = "error"
                self.error = "进程已退出，" + (self.tail(3)[-200:] or "看日志")
                return
            try:
                r = httpx.get(f"http://127.0.0.1:{self.port}/v1/models", timeout=2)
                if r.status_code == 200:
                    self.state = "ready"
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1.0)
        self.state = "error"
        self.error = f"{timeout:.0f} 秒内没就绪"

    # ---------------------------------------------------------------- 停止
    def _terminate(self, pid: int, port: int, timeout: float) -> bool:
        """先 SIGTERM 再 SIGKILL，每一步都验证端口真的关了。

        mlx_lm.server 实测不响应 SIGTERM（可能是 Metal 上有阻塞调用），
        只发 SIGTERM 会静默失败——用户看到"已停止"但 16 GB 还占着。
        所以必须：发信号 → 等 → 查端口 → 不行就升级到 SIGKILL → 再查。
        """
        for sig, wait in ((signal.SIGTERM, 4.0), (signal.SIGKILL, 4.0)):
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            deadline = time.time() + wait
            while time.time() < deadline:
                if not _port_open(port):
                    return True
                time.sleep(0.2)
        return not _port_open(port)

    def stop(self, timeout: float = 15) -> dict:
        pid = self.proc.pid if self.proc else self.adopted_pid
        port = self.port or DEFAULT_PORT
        if not pid:
            self.state = "stopped"
            self._stopped_at = time.time()
            return {"state": "stopped"}

        released = self._terminate(pid, port, timeout)
        self.proc = None
        self.adopted_pid = None
        self.port = 0
        self.state = "stopped" if released else "error"
        self._stopped_at = time.time()
        if not released:
            self.error = f"进程 {pid} 未能退出，端口 {port} 仍被占用（可能需要 kill -9）"
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None
        return {"state": self.state, "pid": pid, "port_released": released}

    # ---------------------------------------------------------------- 观测
    def status(self) -> dict:
        if self.proc is None and self.state == "stopped":
            self.adopt_orphan()          # 懒接管，避免重启后 UI 状态错乱
        adopted_alive = bool(self.adopted_pid and _port_open(self.port or DEFAULT_PORT))
        # 注意：接管来的进程没有 Popen 对象，只看 proc 会把它误判成"已死"
        alive = bool(self.proc and self.proc.poll() is None) or adopted_alive
        if not alive and self.state in ("ready", "starting"):
            self.state = "error" if self.state == "ready" else "stopped"
        info = {
            "state": self.state,
            "model": self.spec.name if self.spec else None,
            "model_id": self.spec.id if self.spec else None,
            "port": self.port,
            "max_tokens": self.max_tokens,
            "pid": (self.proc.pid if alive and self.proc else
                    (self.adopted_pid if adopted_alive else None)),
            "uptime_s": round(time.time() - self.started_at, 1) if alive else 0,
            "error": self.error,
            "rss_gb": 0.0,
            "port_open": _port_open(self.port or DEFAULT_PORT),
            "adopted": bool(self.adopted_pid and adopted_alive),
        }
        target_pid = (self.proc.pid if alive and self.proc else
                      (self.adopted_pid if adopted_alive else None))
        if target_pid:
            try:
                p = psutil.Process(target_pid)
                rss = p.memory_info().rss
                for child in p.children(recursive=True):
                    try:
                        rss += child.memory_info().rss
                    except psutil.Error:
                        pass
                info["rss_gb"] = round(rss / 1024**3, 2)
                info["cpu_pct"] = _process_cpu(target_pid)
            except psutil.Error:
                pass
        vm = psutil.virtual_memory()
        info["sys"] = {
            "total_gb": round(vm.total / 1024**3, 1),
            "used_gb": round((vm.total - vm.available) / 1024**3, 1),
            "free_pct": round(vm.available / vm.total * 100, 1),
            # 模块级函数，内部维护上次采样点，首次调用返回 0
            "cpu_pct": psutil.cpu_percent(interval=None),
            "cores": psutil.cpu_count(),
        }
        return info

    def tail(self, lines: int = 200) -> str:
        if not self._log_path or not self._log_path.exists():
            return ""
        try:
            with self._log_path.open("r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-lines:])
        except OSError:
            return ""

    def log_path(self) -> str:
        return str(self._log_path) if self._log_path else ""


_cpu_proc: "psutil.Process | None" = None
_cpu_pid: int | None = None


def _process_cpu(pid: int) -> float:
    """进程 CPU 占用。

    psutil 的 cpu_percent 是"两次调用之间的增量"，必须复用同一个 Process 对象。
    每次 new 一个 Process 会永远返回 0.0——界面上的 CPU 曲线就一直是平线。
    """
    global _cpu_proc, _cpu_pid
    try:
        if pid != _cpu_pid:
            _cpu_proc = psutil.Process(pid)
            _cpu_pid = pid
            _cpu_proc.cpu_percent(None)      # 建基准，本次返回 0
            return 0.0
        return round(_cpu_proc.cpu_percent(None), 1)
    except (psutil.Error, OSError):
        return 0.0


runner = Runner()
