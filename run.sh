#!/bin/bash
# 启动 Model Studio。
#   bash run.sh                → 只允许本机访问（127.0.0.1:8600）
#   bash run.sh 8600 0.0.0.0   → 允许局域网访问（必须有 MODEL_STUDIO_TOKEN）
cd "$(dirname "$0")" || exit 1
PORT="${1:-8600}"
HOST="${2:-127.0.0.1}"

if [ "$HOST" != "127.0.0.1" ] && [ -z "${MODEL_STUDIO_TOKEN:-}" ]; then
  # .env 里的也算：启动前先读一遍
  TOKEN_IN_ENV=$(grep -E "^MODEL_STUDIO_TOKEN=" .env 2>/dev/null | cut -d= -f2-)
  if [ -z "$TOKEN_IN_ENV" ]; then
    echo "拒绝启动：绑定到 $HOST 但没有 MODEL_STUDIO_TOKEN，局域网里任何人都能用你的模型。"
    echo "先执行：echo 'MODEL_STUDIO_TOKEN=你的口令' >> .env"
    exit 1
  fi
  export MODEL_STUDIO_TOKEN="$TOKEN_IN_ENV"
fi

# ---------------------------------------------------------------- 知识库（algo-rag）
# 知识库由 app 进程在 startup 时自己拉起（见 app/main.py 的 _ensure_rag），
# 这样不管是谁启动 app（launchd / run.sh / IDE），知识库都跟着起来。
# 这里只做提示，不抢着启动——两边同时起会撞 8601 端口，还会留下孤儿 subshell。
RAG_PORT="${RAG_PORT:-8601}"
if curl -s -m 2 "http://127.0.0.1:$RAG_PORT/health" >/dev/null 2>&1; then
  echo "知识库已在运行（:$RAG_PORT）"
else
  echo "知识库未运行 → app 启动后会自动拉起（首次加载约 20~100 秒）"
fi

echo "监听 $HOST:$PORT  ${MODEL_STUDIO_TOKEN:+[已开启 token 校验]}"
# 不用 exec：exec 会替换掉当前进程，上面的 EXIT trap 就不会触发了。
.venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT" --log-level info

