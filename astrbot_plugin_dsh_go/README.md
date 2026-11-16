# astrbot_plugin_dsh_go

在 QQ（或任何 AstrBot 平台）里遥控手机上的 **dsh-go**。

你发一句话 → 插件交给手机上的 dsh-go → dsh-go 用它的工具（读写文件、跑命令、查网页、看仓库）干活 → 结果回到聊天。

这样，聊天机器人本身做不到的事，也能直接在聊天里完成。

---

## 它怎么工作

```
QQ 消息
  ↓
AstrBot（插件解析 /dsh 指令，或聊天模型自己调用 dsh_go 工具）
  ↓  ACP 协议（JSON-RPC over stdio）
dsh --profile acp     ← 手机 dsh-go 的本机引擎，常驻一个子进程
  ↓
bash / 读写文件 / 网页搜索 / 网页抓取 / 子代理 …
  ↓
结果文本 → 发回聊天
```

- **常驻子进程**：引擎只起一次，之后每条指令 1~4 秒返回；空闲 20 分钟自动关掉。
- **会话延续**：同一个群/私聊对应同一段 dsh 上下文；进程重启会自动 `session/resume` 接上。
- **自动同意授权**：dsh-go 需要授权时插件自动同意（可关），否则任务会卡住等人点。
- **超时自愈**：等太久会先中断再报错，不会把后续指令一直堵住。

## 安装

整目录放进 AstrBot 的插件文件夹：

```
<AstrBot>/data/plugins/astrbot_plugin_dsh_go/
├── main.py
├── dsh_client.py
├── _conf_schema.json
├── metadata.yaml
├── __init__.py
└── README.md
```

然后重启 AstrBot，或在 WebUI「插件管理」里重载。

前提：**AstrBot 和 dsh-go 在同一台机器上**，并且 `dsh` 命令能用：

```bash
dsh --version
```

## 两种连接方式

插件支持两种模式，看 AstrBot 和 dsh-go 是不是同一个 Linux 环境：

### 模式 A：本机模式（默认）
AstrBot 和 dsh-go 装在**同一台机器**、同一个 Linux 环境里，插件直接调用 `dsh`。
什么都不用配，`bridge_url` 留空即可。

### 模式 B：桥接模式
AstrBot 装在**另一个环境**（典型例子：安卓「泡泡版」App，它自带一套独立 Linux，
里面没有 dsh）。这时在 dsh-go 那一侧跑一个桥接服务，两边通过本机回环通信。

**第一步：在 dsh-go 这边启动桥接服务**

```sh
sh /root/dsh-go-bridge/启动.sh
```

它会打印地址和口令，例如：

```
地址：http://127.0.0.1:6799
口令：uemX-GW-nCde4XvWN0gEtVu_
```

口令存在 `/root/.dsh-bridge-token`，重复执行启动脚本不会变。

**第二步：在 AstrBot 插件配置里填两项**

| 配置项 | 填什么 |
| --- | --- |
| `bridge_url` | `http://127.0.0.1:6799` |
| `bridge_token` | 启动脚本打印的那个口令 |

填完保存，发 `/dsh 状态`，显示「模式：桥接」就通了。

> 其余配置项（dsh_bin、patch_files、permission_mode 等）在桥接模式下**不生效**，
> 它们由桥接服务那边的 `dsh_bridge.py` 负责。

**桥接服务会自动启动**

已经配好了自启，不用手动管：

- 每次 DSH 引擎启动（= 打开 DSH App），会自动把桥接服务拉起来
- 万一没起来，打开 DSH 的终端敲个回车也会自动拉起
- 手动兜底：`sh /root/dsh-go-bridge/启动.sh`

## 使用

| 输入 | 效果 |
| --- | --- |
| `/dsh 帮我看看 ~/notes.md 写了什么` | 交给 dsh-go 执行并回结果 |
| `/dshgo 查一下今天的科技新闻` | 同上（别名） |
| `/dsh 状态` | 引擎连没连上、工作目录、权限模式 |
| `/dsh 新对话` | 换一段干净的上下文 |
| `/dsh 停止` | 打断正在跑的任务 |
| `/dsh 帮助` | 命令说明 |

不带指令也可以：聊天模型会自己判断要不要调用 `dsh_go` 工具（可在配置里关掉）。

## 配置（WebUI 插件设置里有中文说明）

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `dsh_bin` | `dsh` | dsh 可执行文件；找不到就填绝对路径 |
| `patch_files` | `~/.dsh/profiles/web/cordis.patch.yml` | 复用手机 App 的模型渠道配置 |
| `work_dir` | `~` | dsh-go 的工作目录 |
| `permission_mode` | `danger-full-access` | 手机 proot 无沙箱后端，只能是这个 |
| `env_api_keys` | 空 | 补 `KEY=VALUE`，每行一个 |
| `model` | 空 | 指定 `渠道/模型`，留空用默认 |
| `auto_approve` | 开 | 自动同意 dsh-go 的权限请求 |
| `enable_llm_tool` | 开 | 允许聊天模型自动调用 |
| `admin_only` | 开 | 只允许 AstrBot 管理员 |
| `allowed_users` / `allowed_groups` | 空 | 白名单，填了优先于 admin_only |
| `session_scope` | `chat` | `chat` 一个群共享上下文，`user` 每人一段 |
| `show_progress` | 开 | 执行中播报「正在用 xxx」 |
| `max_reply_chars` | 3500 | 回聊天的最长字数 |
| `bridge_url` | 空 | 桥接模式地址；填了就走桥接，留空走本机 |
| `bridge_token` | 空 | 桥接服务口令 |
| `timeout_seconds` | 420 | 单任务超时 |
| `idle_shutdown_minutes` | 20 | 空闲多久关引擎，0 表示常开 |
| `keep_context` | 开 | 保留上下文 |

### 为什么权限必须是 danger-full-access

手机上的 DSH 跑在 proot 里，没有 bubblewrap / Landlock，`workspace-write` 沙箱后端不可用，命令会被直接拒绝：

```
sandbox mode "workspace-write" is requested but no sandbox backend is usable on this host
```

所以默认 `permission_mode = danger-full-access`，同时靠 `admin_only` + 白名单限制谁能用。

## 安全提醒

开了这个插件，就等于能在聊天里让手机执行命令。建议保持：

- `admin_only: true`（只自己能用）
- 不要把这个指令开放到公共群
- 想更严就把 `allowed_users` 填成自己的 QQ 号

## 排障

| 现象 | 处理 |
| --- | --- |
| `连不上手机上的 dsh-go` | 检查 `dsh --version`，必要时把 `dsh_bin` 写成绝对路径 |
| 一直没反应 | `/dsh 状态`；AstrBot 日志搜 `[dsh-go]` |
| 回复慢 | 首次启动 6~8 秒，之后 1~4 秒；别重复发同一条 |
| 说没权限 | `permission_mode` 改成 `danger-full-access` |
| 模型报错 | `patch_files` 指向手机 App 的模型配置，或填 `env_api_keys` |
| 任务超时 | 调大 `timeout_seconds`；或 `/dsh 停止` 后重发 |

日志关键词：`[dsh-go]`。
