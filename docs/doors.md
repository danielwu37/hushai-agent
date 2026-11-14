# 各端怎么接入（doors）

> 「端」是使用者和 AI 进出记忆库的入口：笔记本上的 AI、聊天机器人、待办页面等。每个端有一个编号，必须列在配置 `[doors] known` 里。
> 各端**不直接改** vault 里的文件，只通过闸门 `hushai gate` 写；闸门输出一律是一个 JSON 对象。

## 1. 三个动作

| 动作 | 命令 | 说明 |
| --- | --- | --- |
| 写收件 | `hushai gate append-inbox --instance X --door D [--auto]` | 标准输入是一行或多行已成形的收件行；逐行校验，合法的写入该端当天的收件文件（日期按服务器时钟和实例时区），写完立即机械对账。单次最多 200 行、每行最长 2000 字符。 |
| 放文件 | `hushai gate put-exchange --instance X --door D --name 文件名` | 标准输入的内容存为 `hub/exchange/in/<端>/<文件名>`。不许带路径、不许以点开头，扩展名和大小受 `[gate]` 限制，同名自动加 `-1`、`-2`，不覆盖。 |
| 查待收 | `hushai gate check-inbox --instance X --door D [--ack]` | 列出 `hub/exchange/out/to-<端>/` 下还没确认过的文件；`--ack` 把本次列出的记为已确认。建议每次会话开始时调用一次。 |

输出示例：

```json
{"accepted": 2, "rejected": [{"line": "…", "reason": "未知或未配置的领域: …"}], "reconciled": {"mechanical": {"mechanical_applied": 1}, "routing": {"skipped": "disabled"}}}
```

全部非法时退出码为 1；`ssh` 方式下命令不被允许时退出码为 2。

## 2. 收件行格式

```text
- HH:MM｜<领域>｜<做了什么>｜产物：<路径或无>｜待办：<下一步或无>
```

分隔符是全角竖线，恰好五段。领域必须是配置里的领域，或保留领域「候选记忆」「更正」。

「做了什么」写成下面的样子时，不经过模型、立即生效：

| 写法 | 效果 |
| --- | --- |
| `完成 260302-1` | 关闭该开放项 |
| `推迟 260302-1 到 2026-03-13` | 改排期，推迟次数加一 |
| `放弃 260302-1` | 标记为不做 |
| `更正 260302-1：<说明>` | 记入更正清单 |
| `批准 C-260305-1 画像` / `批准 C-260305-1 规则` | 候选记忆原文记入画像或补充规则 |
| `丢弃 C-260305-1` | 丢弃该候选 |

批准、丢弃只有 `[approval] doors` 里的端（缺省是管家端和 `web`）写的才生效，其他端写的会进日报的「未对上号」并注明「该端无权批准」。

## 3. 给另一个端留言

「待办」段以 `@<目标>` 开头，表示请目标处理：

```text
- 10:20｜工作｜写了迁移方案初稿｜产物：hub/exchange/in/laptop/方案.md｜待办：@steward 审阅这份方案并列出执行清单
```

- 只有 `[routing] enabled = true`、来源端在 `trusted_doors` 里、目标在 `[routing]` 里配了回退链时才会触发；`-auto` 文件里的行不触发。
- 作答的模型**只出文字、没有任何工具**，不能读写文件。「产物」指向 vault 内不超过 60 KB 的文件时，内容会附给它（`local_only` 里的路径不会）。
- 回复写到 `hub/exchange/out/to-<来源端>/R-<行编号>.md`，同时管家端写一条回执收件行。来源端下次 `check-inbox` 就能看到。
- 同一行失败后最多重试 2 次，之后记 `needs_human`；每天总次数受 `max_daily` 限制，同时受每日调用预算限制。

## 4. 本机的端

和 vault 在同一台机器上的端直接调命令：

```bash
printf -- '- 10:20｜工作｜整理了会议纪要｜产物：无｜待办：无\n' | hushai gate append-inbox --instance X --door laptop
```

## 5. 远端的端（SSH 强制命令）

在服务器上运行 vault 的那个账号的 `authorized_keys` 里，给每个端单独一把密钥，并写死强制命令：

```text
command="<hushai 的绝对路径> gate ssh --instance <实例名> --door <端编号>",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA…<该端的公钥>
```

远端这样用：

```bash
printf -- '- 10:20｜工作｜…｜产物：无｜待办：无\n' | ssh <服务器别名> append-inbox
ssh <服务器别名> put-exchange --name 方案.md < 方案.md
ssh <服务器别名> check-inbox --ack
```

对方只能给出 `append-inbox [--auto]`、`put-exchange --name <文件名>`、`check-inbox [--ack]` 三种写法，多一个参数都会被拒绝；实例名和端编号只来自强制命令本身，对方改不了。

## 6. 只能读写文件、不能跑命令的端

这类端（例如只能通过网盘连接器读写的聊天助手）读 `hub/reports/morning-brief.md`，按文末「怎么回写」的写法，在自己端当天的收件文件 `hub/inbox/YYYY-MM-DD-<端>.md` 里追加一行。服务器上的目录监听发现文件变化后自动对账。注意：经网盘同步过来的改动，要等同步工具把文件落到服务器本地盘才会触发；挂载式的网盘目录收不到变更通知，这种情况靠每日汇总兜底。
