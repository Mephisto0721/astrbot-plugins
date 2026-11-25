# astrbot_plugin_simple_memory — 长期记忆

给机器人加一层**跨会话的长期记忆**：从日常对话里提取「关于用户的稳定事实」存进 SQLite，
之后每轮回答都注入提示词，让它真的记得住人。

记忆按**时效**分成两类：长期事实（fact）和临时事件（event）。

## 依赖

- AstrBot 4.x
- `openai>=1.0`
- 一个 DeepSeek API Key

## 工作方式

```
回复后提取（每 N 轮一次）
  ├─ 把「用户这句话 + 机器人回复」交给大模型
  ├─ 要求按 [fact] / [event] 两种格式输出
  ├─ event 必须把「今天 / 明天」换算成 MM-DD
  └─ 相似度 ≥ 0.85 视为重复，跳过

请求前注入（每 5 轮刷新一次，其余轮复用缓存）
  ├─ 【长期记住的】最近 20 条 fact
  ├─ 【最近几天发生的】最近 3 天的 event，带 [MM-DD]
  └─ 【和当前话题相关的】按关键词匹配的 fact

刷新时顺手归档
  └─ 超过 7 天的 event 标 archived（不删除，只是不再注入）
```

### 为什么分 fact / event

「我今天有课」如果存成永久事实，一周后读到就不知道是哪天说的。
所以 event 在**写入那一刻**就把日期固化进 `event_date`，之后无论哪天读都准确。

### 归档不是删除

`status='archived'` 只是不再注入、不占 token，数据仍在库里，随时可查。

## 配置

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `deepseek_api_key` | string | 空 | DeepSeek API Key |
| `deepseek_base_url` | string | `https://api.deepseek.com/v1` | API 地址 |
| `deepseek_model` | string | `deepseek-chat` | 提取记忆用的模型 |
| `only_private` | bool | `true` | 只在私聊生效（推荐开启） |
| `extract_every_n_turns` | int | 3 | 每 N 轮提取一次（调大可省 token） |

## 命令

```
/mem list            列出全部记忆
/mem count           显示条数
/mem search 关键词    搜索
/mem add 内容         手动添加
/mem del ID           删除指定 ID
/mem clear 确认       清空全部
```

## 提供给大模型的工具

| 工具 | 作用 |
|---|---|
| `remember` | 主动写入一条稳定事实 |
| `recall_memory` | 按关键词检索记忆（只搜 fact） |
| `memory_stats` | 查看记忆条数 |

## 数据

`memory.db`（SQLite，首次启动自动创建）：

```sql
memories(
  id, content, created_at,
  kind,         -- fact / event
  event_date,   -- event 发生日期 YYYY-MM-DD
  status,       -- active / archived
  last_used_at  -- 被注入过的时间，留给以后做冷热排序
)
```

旧库启动时会自动 `ALTER TABLE` 补列，历史数据默认算作 `fact`。

> 数据库里是提取出来的私人事实，**不要提交进仓库**。

## 一点提醒

提取用的是大模型，偶尔会判成「无」（输出只有一个 token）。
这通常是那一轮对话本身没信息量（比如「嗯」「对」），不是故障。
连续多轮都不写入时，才需要检查 `extract_every_n_turns`。
