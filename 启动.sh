#!/bin/sh
# 启动 dsh-go 桥接服务（重复执行安全）
# 用法：sh 启动.sh [端口] [口令]
PORT="${1:-6799}"
TOKEN="${2:-$(cat /root/.dsh-bridge-token 2>/dev/null)}"
DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$TOKEN" ]; then
  TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
  echo "$TOKEN" > /root/.dsh-bridge-token
  chmod 600 /root/.dsh-bridge-token
fi

if curl -s -m 2 -H "X-DSH-Token: $TOKEN" "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "桥接服务已经在跑：http://127.0.0.1:$PORT"
  echo "口令：$TOKEN"
  exit 0
fi

setsid nohup python3 "$DIR/dsh_bridge.py" --port "$PORT" --token "$TOKEN" \
  >> /root/dsh_bridge.log 2>&1 < /dev/null &
sleep 3

if curl -s -m 3 -H "X-DSH-Token: $TOKEN" "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "桥接服务已启动"
  echo "地址：http://127.0.0.1:$PORT"
  echo "口令：$TOKEN"
else
  echo "启动失败，看 /root/dsh_bridge.log："
  tail -20 /root/dsh_bridge.log 2>/dev/null
  exit 1
fi
