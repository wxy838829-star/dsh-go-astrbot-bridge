#!/bin/sh
# 启动 dsh-go 桥接服务（重复执行安全）
# 用法：sh 启动.sh [端口] [口令]
PORT="${1:-6799}"
TOKEN="${2:-$(cat /root/.dsh-bridge-token 2>/dev/null)}"
DIR="$(cd "$(dirname "$0")" && pwd)"
CRED="/root/.dsh/.credentials.yaml"

if [ -z "$TOKEN" ]; then
  TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")
  echo "$TOKEN" > /root/.dsh-bridge-token
  chmod 600 /root/.dsh-bridge-token
fi

# 从 DSH 的凭证文件里读出模型 API Key 注入环境。
# 不注入的话，桥接起的 dsh 引擎拿不到 key，会报 Insufficient Balance。
if [ -f "$CRED" ]; then
  for kv in $(python3 - "$CRED" <<'PY'
import re, sys
try:
    text = open(sys.argv[1], encoding="utf-8").read()
except Exception:
    sys.exit(0)
# 只取 refs: 下面形如 KEY: sk-xxx 的行
for line in text.splitlines():
    m = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*:\s*(\S+)\s*$", line)
    if m and "API_KEY" in m.group(1):
        print(f"{m.group(1)}={m.group(2)}")
PY
); do
    export "$kv"
  done
fi

if curl -s -m 2 -H "X-DSH-Token: $TOKEN" "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "桥接服务已经在跑：http://127.0.0.1:$PORT"
  echo "口令：$TOKEN"
  echo "提示：如果模型报余额不足，先停掉再重启本脚本，让 Key 生效"
  exit 0
fi

setsid nohup python3 "$DIR/dsh_bridge.py" --port "$PORT" --token "$TOKEN" \
  >> /root/dsh_bridge.log 2>&1 < /dev/null &
sleep 3

if curl -s -m 3 -H "X-DSH-Token: $TOKEN" "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "桥接服务已启动"
  echo "地址：http://127.0.0.1:$PORT"
  echo "口令：$TOKEN"
  n=$(python3 -c "
import os
c=0
for kv in open('/proc/self/environ','rb').read().decode('utf-8','replace').split('\0'):
    if 'API_KEY' in kv: c+=1
print(c)
")
  echo "已注入 API Key 数量：$n"
else
  echo "启动失败，看 /root/dsh_bridge.log："
  tail -20 /root/dsh_bridge.log 2>/dev/null
  exit 1
fi
