# dsh-go × AstrBot 桥接

在 QQ（或任何 AstrBot 平台）里遥控手机上的 **dsh-go**。

你在聊天里发一句话 → AstrBot 插件把它转给手机上的 dsh-go → dsh-go 用它的工具（读写文件、跑命令、查网页）干活 → 结果回到聊天。

聊天机器人本身做不到的事（改文件、执行命令、查网页），也能直接在聊天里完成。

```
QQ 消息
  ↓
AstrBot（/dsh 指令，或聊天模型自己调用 dsh_go 工具）
  ↓  本机模式：直接调 dsh
  ↓  桥接模式：HTTP → dsh-go 那一侧
dsh --profile acp     ← 手机 dsh-go 的本机引擎，常驻子进程
  ↓
bash / 读写文件 / 网页搜索 / 网页抓取 / 子代理 …
  ↓
结果文本 → 发回聊天
```

## 这个仓库有什么

```
astrbot_plugin_dsh_go/    AstrBot 插件（Python）
dsh-go-bridge/            桥接服务（跑在 dsh-go 那一侧）
```

### AstrBot 插件 `astrbot_plugin_dsh_go`

- `/dsh <指令>` 把指令交给 dsh-go；`/dsh 状态`、`/dsh 新对话`、`/dsh 停止`、`/dsh 帮助`
- 注册 `dsh_go` 工具，聊天模型可以自己判断要不要转给 dsh-go
- 用 **ACP 协议**（JSON-RPC over stdio）驱动 `dsh --profile acp`，引擎常驻，每条指令 1~4 秒
- 同一段聊天保持上下文，进程重启后自动 `session/resume` 接上
- 空闲自动关引擎省资源

### 桥接服务 `dsh-go-bridge`

AstrBot 有时装在**另一个 Linux 环境**里（例如安卓「泡泡版」App 自带一套独立容器，里面没有 `dsh`）。这时在 dsh-go 一侧跑桥接服务，两边通过本机回环通信。

- 只监听 `127.0.0.1`，带口令校验
- 自带权限/超时/取消处理，引擎挂了会自动重拉
- `autostart.mjs` 可挂到 DSH 引擎启动流程，实现开机自启

## 两种连接方式

| 模式 | 什么时候用 | 怎么配 |
| --- | --- | --- |
| **本机** | AstrBot 和 dsh-go 在同一台机器、同一 Linux 环境 | `bridge_url` 留空 |
| **桥接** | AstrBot 在另一个容器/机器 | 填 `bridge_url` + `bridge_token` |

## 快速开始

### 1. 装 AstrBot 插件

把 `astrbot_plugin_dsh_go/` 整个目录放进 AstrBot 的 `data/plugins/`，重启或在 WebUI 重载。

### 2A. 本机模式

什么都不用配，确认 `dsh --version` 能跑即可。

### 2B. 桥接模式

在 dsh-go 那一侧：

```sh
git clone <this-repo>
cd dsh-go-astrbot-bridge/dsh-go-bridge
sh 启动.sh
```

会打印：

```
桥接服务已启动
地址：http://127.0.0.1:6799
口令：xxxxxxxx
```

把地址和口令填进 AstrBot 插件的 `bridge_url` 和 `bridge_token`。

### 3. 用

```
/dsh 帮我看看 ~/notes.md 写了什么
/dsh 查一下今天的科技新闻
/dsh 状态
```

## 配置要点

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `bridge_url` | 空 | 桥接模式地址，如 `http://127.0.0.1:6799` |
| `bridge_token` | 空 | 桥接服务口令 |
| `permission_mode` | `danger-full-access` | 见下 |
| `admin_only` | 开 | 只允许 AstrBot 管理员 |
| `allowed_users` | 空 | 用户白名单，比 admin_only 更严 |
| `session_scope` | `chat` | `chat` 一个群共享上下文，`user` 每人一段 |
| `timeout_seconds` | 420 | 单任务超时 |

完整配置见 [插件 README](astrbot_plugin_dsh_go/README.md)。

### 为什么权限必须是 danger-full-access

手机上的 DSH 跑在 proot 里，没有 bubblewrap / Landlock，`workspace-write` 沙箱后端不可用，命令会被直接拒绝：

```
sandbox mode "workspace-write" is requested but no sandbox backend is usable on this host
```

所以默认 `danger-full-access`，同时靠 `admin_only` + 白名单限制谁能用。

## 安全提醒

**这个插件等于「能在聊天里让手机执行命令」。** 请务必：

- 保持 `admin_only: true`
- 不要开放到公共群
- 想更严就把 `allowed_users` 填成自己的账号
- 桥接模式不要改成监听 `0.0.0.0`

## 已知限制

- 只支持 DSH 的 ACP profile（`dsh --profile acp`）
- 手机重启后桥接需要重新拉起（可用 `autostart.mjs` 挂到引擎启动，见 [桥接说明](dsh-go-bridge/使用说明.md)）
- 长任务建议调大 `timeout_seconds`

## License

MIT
