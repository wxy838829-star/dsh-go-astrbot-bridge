# dsh-go 桥接服务

给 [astrbot_plugin_dsh_go](https://github.com/wxy838829-star/astrbot_plugin_dsh_go) 用的一条跨容器通道。

## 为什么需要它

AstrBot 有时装在**另一个 Linux 环境**里 —— 典型例子是安卓「泡泡版」App，它自带一套独立容器，里面没有 `dsh`。

而 dsh-go 跑在它自己的环境里。两个容器**共享同一个网络栈**，所以 AstrBot 能通过 `127.0.0.1` 找到这个桥接服务。

```
AstrBot（容器 A）
   ↓ HTTP 127.0.0.1:6799
桥接服务（容器 B，dsh-go 所在环境）
   ↓ ACP（JSON-RPC over stdio）
dsh --profile acp
```

> 如果 AstrBot 和 dsh-go 本来就在同一台机器、同一个 Linux 环境里，
> **不需要这个服务**，插件直接用「本机模式」调用 `dsh` 就行。

## 安装

放到 dsh-go 所在的环境里，建议路径 `/root/dsh-go-bridge`：

```sh
git clone https://github.com/wxy838829-star/dsh-go-astrbot-bridge.git /root/dsh-go-bridge
cd /root/dsh-go-bridge
sh 启动.sh
```

输出：

```
桥接服务已启动
地址：http://127.0.0.1:6799
口令：xxxxxxxxxxxxxxxx
```

把**地址**和**口令**填进 AstrBot 插件的 `bridge_url` 和 `bridge_token`。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `dsh_bridge.py` | 桥接服务本体（aiohttp） |
| `dsh_client.py` | ACP 客户端，驱动 `dsh --profile acp` |
| `启动.sh` | 一键启动（幂等，重复执行安全） |
| `autostart.mjs` | 可选：挂到 DSH 引擎启动流程，实现开机自启 |
| `使用说明.md` | 更详细的使用/排障说明 |

## 接口

| 路径 | 作用 |
| --- | --- |
| `GET /health` | 健康检查 |
| `POST /ask` | `{text, session}` → `{ok, text, error, elapsed, tools}` |
| `POST /reset` | `{session}` 丢弃上下文 |
| `POST /stop` | `{session}` 中断任务 |

所有请求需带 `X-DSH-Token` 头（或 `?token=`）。

## 自动启动

可选。把 `autostart.mjs` 挂到 DSH 的 web profile 里，引擎每次启动就会顺手拉起桥接：

```yaml
# ~/.dsh/profiles/web/cordis.patch.yml
- insert:
    - id: dsh-go-bridge-autostart
      name: /root/dsh-go-bridge/autostart.mjs
```

`autostart.mjs` 顶部有 `SCRIPT` 常量，路径不同时改一下。插件全程 try/catch，
即使脚本出问题也只记一行日志，**不会影响 DSH 引擎启动**。

## 安全

- 默认只监听 `127.0.0.1`，局域网访问不到
- 有口令校验，口令存在 `/root/.dsh-bridge-token`（600 权限）
- 它等价于「能在手机上执行命令」，**不要**改成监听 `0.0.0.0`

## License

MIT
