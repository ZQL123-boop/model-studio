"""一次性工具路由：加载 0.5B 路由器 → 分类 → 输出 JSON → 退出。

给主程序用（主程序的 .venv 没有 mlx-lm），必须用装了 mlx-lm 的 python 跑：

    /Users/ziqilin/Projects/qwen-local/.venv/bin/python training/route_once.py "中山天气怎么样"

stdout 最后一行是 JSON：{"tool": "...", "args": {...}, "need_llm_args": bool, "raw": "..."}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))            # 让 `from router import Router` 生效
sys.path.insert(0, str(HERE.parent))     # router.py 内部也会把项目根塞进 sys.path

from router import Router  # noqa: E402


def main() -> None:
    q = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read().strip()
    if not q:
        print(json.dumps({"tool": None, "args": {},
                          "need_llm_args": True, "raw": ""}, ensure_ascii=False))
        return
    r = Router()
    out = r.route(q)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
