#!/bin/bash
# Model Studio 一键启动
#   知识库(8601) + 界面(8600) + 自动打开浏览器
# 用法：在访达里双击本文件。关掉终端窗口或按 Ctrl+C 停止服务。

set -u
MS="$HOME/Projects/model-studio"
RAG="$HOME/Projects/algo-rag"
LOG="$MS/logs"
mkdir -p "$LOG"

# 双击运行时工作目录是 ~，先切到 HOME 免得相对路径出错
cd "$HOME" || exit 1

STARTED=""

cleanup() {
  if [ -n "$STARTED" ]; then
    echo ""
    echo "正在停止服务…"
    kill $(jobs -p) 2>/dev/null
    wait 2>/dev/null
    echo "已停止。"
  fi
}
trap cleanup EXIT INT TERM

port_open() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t >/dev/null 2>&1; }

clear
echo "  Model Studio"
echo "  ────────────────────────────────────────"
echo ""

# ---------- 知识库 ----------
if port_open 8601; then
  echo "  ✓ 知识库已在运行（8601）"
else
  if [ ! -x "$RAG/.venv/bin/python" ]; then
    echo "  ✗ 找不到 $RAG/.venv/bin/python"
  else
    echo "  → 启动知识库（加载 bge-m3 约 40 秒）"
    ( cd "$RAG" && exec ./.venv/bin/python serve.py 8601 ) \
      > "$LOG/rag.out.log" 2>&1 &
    STARTED="1"
  fi
fi

# ---------- 界面 ----------
if port_open 8600; then
  echo "  ✓ 界面已在运行（8600）"
else
  if [ ! -f "$MS/run.sh" ]; then
    echo "  ✗ 找不到 $MS/run.sh"
    read -r -p "  按回车关闭…" _
    exit 1
  fi
  echo "  → 启动界面"
  ( cd "$MS" && exec bash run.sh 8600 0.0.0.0 ) \
    > "$LOG/studio.out.log" 2>&1 &
  STARTED="1"
fi

# ---------- 等界面 ----------
printf "  → 等待界面"
ok=0
i=0
while [ "$i" -lt 30 ]; do
  if curl -s -m 1 http://127.0.0.1:8600/api/health >/dev/null 2>&1; then
    ok=1; break
  fi
  printf "."
  sleep 1
  i=$((i + 1))
done
if [ "$ok" = "1" ]; then echo " ✓"; else echo " ✗"; fi

open "http://127.0.0.1:8600" >/dev/null 2>&1

echo ""
echo "  ────────────────────────────────────────"
echo "  界面    http://127.0.0.1:8600"
echo "  知识库  http://127.0.0.1:8601/health"
echo "  日志    $LOG/"
echo "  ────────────────────────────────────────"

# ---------- 播报知识库进度 ----------
i=0
while [ "$i" -lt 60 ]; do
  ST=$(curl -s -m 2 http://127.0.0.1:8601/health 2>/dev/null \
       | sed -n 's/.*"state": *"\([a-z]*\)".*/\1/p')
  case "$ST" in
    ready)   echo "  知识库就绪 ✓"; break ;;
    error)   echo "  知识库加载失败，看 $LOG/rag.out.log"; break ;;
    loading) printf "." ;;
  esac
  sleep 2
  i=$((i + 1))
done

if [ -n "$STARTED" ]; then
  echo ""
  echo "  服务运行中。关掉这个窗口或按 Ctrl+C 停止。"
  echo ""
  wait
else
  echo ""
  echo "  服务本来就在跑，这个窗口可以关掉了。"
  echo ""
  read -r -p "  按回车关闭…" _
fi
