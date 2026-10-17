# astrbot_plugin_ledger — 私人账本

给机器人一本属于它自己的账本，记三样东西：

- **promise（约定）**：和用户之间的约定
- **wish（愿望）**：它自己许下的愿望
- **thought（心事）**：它自己的情绪、想记下来的念头

账本每轮对话都会注入提示词（最近几条 + 快到期的几条），机器人可以用工具自由读写。
带 `due_at` 的条目到期后会被推送给 `astrbot_plugin_proactive_contact`，在合适的时机被提醒。

## ⚠️ 依赖

本插件依赖 **astrbot_plugin_proactive_contact**——"到期提醒"是由它来发出去的。
没装它的话，账本本身照常读写，只是到期提醒不会送达。
`proactive_db_path` 留空时会自动探测同级目录下的 `astrbot_plugin_proactive_contact/proactive.db`。

## 配置

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `inject_recent_count` | int | 3 | 每轮注入最近几条账本条目 |
| `inject_due_count` | int | 3 | 每轮注入几条快到期的条目 |
| `due_soon_hours` | int | 24 | 多久内算"快到期"（小时） |
| `scan_interval_seconds` | int | 60 | 后台扫描到期条目的间隔（秒） |
| `proactive_db_path` | string | 空 | astrbot_plugin_proactive_contact 的数据库路径（留空自动探测） |

## 命令

```
/ledger list [分类]        查看账本（promise / wish / thought）
/ledger add <分类> <内容>   手动添加一条
/ledger doing <ID>         标记为进行中
/ledger active <ID>        标记为生效中
/ledger done <ID>          标记为已实现
/ledger void <ID>          标记为作废
/ledger del <ID>           删除
/ledger search <关键词>     搜索
/ledger due <ID> <时间>     设置到期时间（YYYY-MM-DD HH:MM）
```

## 状态说明

| 状态 | 含义 |
|---|---|
| `in_progress` | 进行中（有终点，通常会带到期时间） |
| `active` | 生效中（长期有效，没有终点，比如"不互相骗对方"） |
| `achieved` | 已实现 |
| `void` | 作废 |

## 提供给大模型的工具

| 工具 | 作用 |
|---|---|
| `ledger_write` | 写一条记录（分类 / 内容 / 到期时间 / 状态） |
| `ledger_list` | 按分类或状态查看 |
| `ledger_update` | 改状态、内容或到期时间 |
| `ledger_search` | 按关键词搜索 |

## 数据

`ledger.db`（SQLite，首次启动自动创建）：

表 `ledger(id, umo, category, content, status, due_at, pushed_at, created_at, updated_at)`

> 账本里是很私人的约定与心事，**不要提交进仓库**。
