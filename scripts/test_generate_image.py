"""generate_image 回归测试：临时限流要在工具内部消化，不能甩给模型。

复现事故（存档 002f5502）：用户说「再生成一张」，Pollinations 连续回
503 / 402，工具把 `[错误] HTTP xxx` 原样回灌，本地 30B 当成「工具做不到」，
转去 web_search ×2 + fetch_url ×3，最后编一句「图像生成服务暂时不可用」。
9 轮 / 158.9s，一张图没出。而服务其实隔几秒就自动恢复。

全部 mock 掉网络，不消耗真实额度。
跑法：.venv/bin/python scripts/test_generate_image.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import tools  # noqa: E402

IMG = b"\xff\xd8\xff" + b"x" * 8000          # 够大，能通过 >=5000 校验


def resp(status: int, content: bytes = IMG) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.content = content
    return m


def run() -> int:
    failed = 0
    total = 0

    def check(cond: bool, label: str):
        nonlocal failed, total
        total += 1
        if not cond:
            print(f"FAIL {label}")
            failed += 1

    with tempfile.TemporaryDirectory() as td:
        with patch.object(tools, "WRITE_ROOT", pathlib.Path(td)):
            # 1) 402 / 503 连续失败，第 4 次成功
            tools._LAST_IMG_PROMPT = None
            with patch("httpx.Client") as C, patch("time.sleep") as slp:
                client = C.return_value.__enter__.return_value
                client.get.side_effect = [resp(402), resp(503), resp(429), resp(200)]
                out = tools.generate_image("一只猫", 256, 256)
                check(out.startswith("[已生成]"), "限流后应重试成功")
                check(client.get.call_count == 4, "应尝试 4 次")
                check([c.args[0] for c in slp.call_args_list] == [3, 6, 12],
                      "退避应为 3/6/12 秒")
                check("![一只猫](" in out, "返回前端可渲染的 Markdown")
                urls = [c.args[0] for c in client.get.call_args_list]
                check(urls[0].startswith("https://image.pollinations.ai/"),
                      "前两次应走主入口")
                check(urls[2].startswith("https://pollinations.ai/p/"),
                      "第 3 次应切备用入口")
                seeds = [c.kwargs["params"]["seed"] for c in client.get.call_args_list]
                check(len(set(seeds)) == 4, "每次重试都要换 seed")

            # 2) 非临时错误不重试
            with patch("httpx.Client") as C, patch("time.sleep") as slp:
                client = C.return_value.__enter__.return_value
                client.get.side_effect = [resp(400)]
                out = tools.generate_image("一只猫", 256, 256)
                check(out.startswith("[错误]") and "不重试" in out, "400 应立即返回不重试")
                check(client.get.call_count == 1, "400 不应重试")
                check(slp.call_count == 0, "400 不应退避")

            # 3) 全部失败：不能只说「服务不可用」，要给明确指令
            with patch("httpx.Client") as C, patch("time.sleep"):
                client = C.return_value.__enter__.return_value
                client.get.side_effect = [resp(402)] * 4
                out = tools.generate_image("一只猫", 256, 256)
                check(out.startswith("[临时故障]"), "全失败应标注临时故障")
                check("不要改用 web_search" in out, "必须禁止模型切去联网找图")
                check("不要向用户编造结果" in out, "必须禁止模型编结论")
                check("再调用一次 generate_image" in out, "必须给出下一步动作")

            # 4) 「再生成一张」复用上一次描述 + 带随机 seed
            tools._LAST_IMG_PROMPT = None
            with patch("httpx.Client") as C:
                client = C.return_value.__enter__.return_value
                client.get.side_effect = [resp(200), resp(200)]
                tools.generate_image("一只橘猫", 256, 256)
                out2 = tools.generate_image("再生成一张", 256, 256)
                check("提示词：一只橘猫" in out2, "指代应复用上一次的 prompt")
                s1 = client.get.call_args_list[0].kwargs["params"]["seed"]
                s2 = client.get.call_args_list[1].kwargs["params"]["seed"]
                check(s1 != s2, "两次 seed 应不同，否则又拿回同一张图")

            # 5) 指代检测别误伤正常描述
            check(tools._is_repeat_prompt("再生成一张"), "'再生成一张' 应判为指代")
            check(tools._is_repeat_prompt("another one"), "'another one' 应判为指代")
            check(not tools._is_repeat_prompt("一只橘猫坐在窗台上晒太阳"),
                  "正常描述不应判成指代")

    print(f"\n{total - failed}/{total} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
