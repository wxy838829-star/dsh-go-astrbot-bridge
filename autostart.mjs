// dsh-go 桥接服务自启插件
//
// 作用：DSH 引擎每次启动时，顺手把 AstrBot 用的桥接服务拉起来。
// 这样手机重启后只要打开 DSH App，桥接就自动在了，不用手动敲命令。
//
// 设计上刻意做到「绝不抛异常」：apply 里所有操作都包在 try/catch 里，
// 失败只打一行日志。这样即使桥接脚本有问题，也不会影响 DSH 引擎启动。
//
// 挂在 ~/.dsh/profiles/web/cordis.patch.yml 的 insert 列表里，
// 由引擎每次启动时按绝对路径加载。文件本身放在 /root/ 下，
// 不随工作区清理而丢失（它一旦缺失，引擎会起不来）。

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'

export const name = 'dsh-go-bridge-autostart'

const SCRIPT = '/root/dsh-go-bridge/启动.sh'

export function apply(ctx) {
  try {
    if (!existsSync(SCRIPT)) {
      console.warn('[dsh-go-bridge] 找不到启动脚本，跳过自启：' + SCRIPT)
      return
    }
    // detached + stdio ignore：桥接自己 setsid 脱离，不占用引擎的输出管道，
    // 引擎重启也不会把它带走。
    const child = spawn('/bin/sh', [SCRIPT], {
      detached: true,
      stdio: 'ignore',
      cwd: '/root/dsh-go-bridge',
    })
    child.on('error', (error) => {
      console.warn('[dsh-go-bridge] 拉起桥接服务失败：' + String(error))
    })
    child.unref()
    console.log('[dsh-go-bridge] 已在引擎启动时拉起桥接服务')
  } catch (error) {
    // 任何意外都只记日志，绝不让引擎启动失败
    console.warn('[dsh-go-bridge] 自启插件异常（已忽略）：' + String(error))
  }
}
