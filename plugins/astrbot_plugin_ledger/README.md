# astrbot_plugin_ledger — 私人账本

给机器人一本属于它自己的账本，记三样东西：

- **promise（约定）**：和用户之间的约定
- **wish（愿望）**：它自己许下的愿望
- **thought（心事）**：情绪、想记下来的念头

每轮对话把「最近几条 + 快到期的几条」注入提示词，机器人可以用工具自由读写。
带 `due_at` 的条目到期后由 `astrbot_plugin_proactive_contact` 推送提醒。

## ⚠️ 依赖

到期提醒依赖 **astrbot_plugin_proactive_contact**。没装它的话账本照常读写，只是提醒不会送达。
`proactive_db_path` 留空时会自动探测同级目录下的 `astrbot_plugin_proactive_contact/proactive.db`。

## 生命周期

```
in_progress  进行中（有终点，做到某事就结束，通常带 due_at）
active       生效中（长期有效，没有终点，例如「不互相骗对方」）
achieved     已实现
void         作废
```

## 配置

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `inject_recent_count` | int | 3 | 每轮注入最近几条 |
| `inject_due_count` | int | 3 | 每轮注入几条快到期的 |
| `due_soon_hours` | int | 24 | 多久内算「快到期」 |
| `scan_interval_seconds` | int | 60 | 后台扫描到期条目的间隔 |
| `proactive_db_path` | string | 空 | proactive 的数据库路径（留空自动探测） |

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

## 提供给大模型的工具

| 工具 | 作用 |
|---|---|
| `ledger_write` | 写一条承诺／愿望／心事 |
| `ledger_list` | 列出账本 |
| `ledger_update` | 改状态或到期时间 |
| `ledger_search` | 按关键词搜 |

## 数据

`ledger.db`（SQLite，首次启动自动创建）：

```sql
ledger(id, umo, category, content, status, due_at, pushed_at, created_at, updated_at)
```

> 账本里是私人约定，**不要提交进仓库**。
