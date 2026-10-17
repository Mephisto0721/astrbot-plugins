# astrbot_plugin_simple_memory — 长期记忆

给机器人加一层**跨会话的长期记忆**：它从日常对话里提取"关于用户的稳定事实"存进 SQLite，
之后每轮回答都把这些记忆注入提示词，让它真的记得住人。

## 依赖

- AstrBot 4.x
- `openai>=1.0`
- 一个 DeepSeek API Key

## 工作方式

1. **回复后提取**
   每 N 轮（默认 3 轮）把这一轮的问答交给大模型，提取关于用户的稳定事实
   （身份、偏好、习惯、重要关系、长期计划、明确表达过的感受）。
   相似度高于 0.85 的重复条目会被跳过，不重复入库。

2. **请求前注入**
   每轮回答前，把「最近 20 条记忆 + 按用户消息关键词匹配到的条目」拼进 system prompt。
   为了命中 DeepSeek 的前缀缓存，注入块每 5 轮才刷新一次，其余轮次复用缓存。

3. **工具补充**
   机器人可以主动调用 `remember` 写一条记忆、`recall_memory` 精确检索。

群聊等场景默认不处理（`only_private`），避免把别人的话也当成"关于用户的事实"记下来。

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
/mem search 关键词   搜索
/mem add 内容        手动添加
/mem del ID          删除指定 ID
/mem clear 确认      清空全部
```

## 提供给大模型的工具

| 工具 | 作用 |
|---|---|
| `remember` | 主动写入一条稳定事实 |
| `recall_memory` | 按关键词检索记忆 |
| `memory_stats` | 查看记忆条数 |

## 数据

`memory.db`（SQLite，首次启动自动创建）：表 `memories(id, content, created_at)`。

> 数据库里是提取出来的私人事实，**不要提交进仓库**。

## 一点提醒

大模型偶尔会把"提取失败"的产物也写成一条（比如「某句话指代不明确，无法提取稳定事实」）。
这类条目留着无害（回答时会被忽略），嫌脏的话可以定期 `/mem list` 人工过一遍。
