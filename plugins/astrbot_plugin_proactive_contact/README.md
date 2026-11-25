# astrbot_plugin_proactive_contact — 主动联系

让机器人在合适的时机**自己去联系用户**，而不是只能等着被找。

后台跑一个定时循环：到点了就把「当前时间 + 今日摘要 + 长期记忆 + 账本 + 最近对话 + 过去的判断」
拼成提示词，交给大模型判断「现在适不适合主动说句话」；适合就发，并由模型自己决定下次何时再判断。

## 依赖

- AstrBot 4.x
- `openai>=1.0`（DeepSeek 的 OpenAI 兼容接口）
- 一个 DeepSeek API Key

## 工作方式

```
后台循环（每 30 秒 tick 一次）
   ↓
有没有到期的 Timer？
   ↓
最近 5 分钟有人在聊？ ——有——→ 跳过（不打断）
   ↓ 没有
压缩「今日摘要」（距上次满 1 小时才做，只送增量）
   ↓
拼提示词 → 大模型判断 reply / no_reply
   ↓
★ 发送前再查一次：判断期间用户又开口了就放弃
   ↓
链式续期：下次隔多久再判断，由模型在 chain_min ~ chain_max 之间决定
```

### 几个设计说明

- **链式 Timer**：每次判断完自动续期，间隔由模型拿捏，「多久找一次」不是写死的。
- **今日摘要**：把当天对话压成一段日记式摘要存库，唤醒时注入，避免「刚聊过的事转头又问一遍」。
  只送增量，没有新消息就不调用模型。
- **二次检查**：从「检查有没有人在聊天」到「真正发消息」之间要跑两次大模型调用（可能几十秒），
  所以发送前会再确认一次，防止插进正在进行的对话。
- **决策记录**：每次判断的结论和理由存进 `decisions`，下次唤醒时作为「过去的判断」注入。
  prompt 里会强调「那只是你当时的判断，不是现在的事实」，避免它照搬旧结论。

## 配置

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `deepseek_api_key` | string | 空 | DeepSeek API Key |
| `deepseek_base_url` | string | `https://api.deepseek.com/v1` | API 地址 |
| `deepseek_model` | string | `deepseek-chat` | 唤醒判断用的模型 |
| `chain_default_interval_minutes` | int | 120 | 链式 Timer 初始间隔 |
| `chain_min_interval_minutes` | int | 15 | 最小间隔 |
| `chain_max_interval_minutes` | int | 720 | 最大间隔 |
| `quiet_hours_start` | int | 23 | 免打扰开始小时（24 制） |
| `quiet_hours_end` | int | 7 | 免打扰结束小时 |
| `history_window` | int | 20 | 唤醒时带最近多少条消息 |

## 命令

```
/proactive on                     在当前会话启用
/proactive off                    停用
/proactive status                 查看启用状态与待触发的 Timer
/proactive fire_now               立即触发一次判断（测试用）
/proactive oneshot <分钟> <事件>   手动安排一次性回访
/proactive cancel                  取消所有未触发的一次性回访
/proactive chain <分钟>            手动调整下次链式判断的时间
```

## 提供给大模型的工具

| 工具 | 作用 |
|---|---|
| `proactive_schedule_oneshot` | 安排一次指定分钟数后的回访 |
| `proactive_cancel_oneshot` | 取消一次性回访（可按关键词） |
| `proactive_adjust_chain` | 调整下次链式判断的时间 |
| `recall_my_decisions` | 查自己过去做过的判断记录 |

## 数据

`proactive.db`（SQLite，首次启动自动创建）：

| 表 | 内容 |
|---|---|
| `messages` | 对话记录 |
| `timers` | 待触发／已触发的定时任务 |
| `decisions` | 每次唤醒的判断结果与理由 |
| `sessions` | 哪些会话启用了主动联系 |
| `daily_context` | 每日摘要（会话 + 日期 + 摘要 + 压缩锚点） |

> 真实对话记录，**不要提交进仓库**。
