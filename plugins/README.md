# AstrBot 陪伴插件集

四个给 [AstrBot](https://docs.astrbot.app/) 用的插件，围绕三件事做的：
**让它记得住人**、**让它会主动找你**、**让它有一本自己的账**。

| 插件 | 作用 | 需要 API Key |
|---|---|---|
| [`astrbot_plugin_proactive_contact`](astrbot_plugin_proactive_contact/) | 定时唤醒机器人，由它自己判断该不该主动联系用户 | ✅ |
| [`astrbot_plugin_simple_memory`](astrbot_plugin_simple_memory/) | 长期记忆：从对话里提取关于用户的稳定事实 | ✅ |
| [`astrbot_plugin_ledger`](astrbot_plugin_ledger/) | 私人账本：约定 / 愿望 / 心事，到期自动提醒 | ❌ |
| [`astrbot_plugin_sticker`](astrbot_plugin_sticker/) | 表情包：它自己判断什么时候发张图更合适 | ❌ |

## 它们怎么配合

```
用户发来消息
   ↓
[simple_memory] 注入长期记忆
[ledger]         注入账本（最近几条 + 快到期的）
   ↓
机器人回答
   ↓
[simple_memory] 提取新的事实
[ledger]         记录新的约定 / 愿望 / 心事
   ↓
[proactive_contact] 定时醒来 → 读「最近对话 + 长期记忆 + 账本 + 今日摘要」
                    → 判断现在适不适合主动说句话
   ↓
[sticker] 聊天过程中，它觉得"这张图比打字合适"就自己发一张
```

- **`astrbot_plugin_ledger` 的到期提醒依赖 `astrbot_plugin_proactive_contact`**（由后者负责发出去）。
  没装也没关系：账本照常读写，只是到期提醒不会送达。
- 其余三个插件彼此独立，可以只装其中任意几个。

## 安装

1. 把需要的插件目录整个复制到 AstrBot 的 `data/plugins/` 下
2. 重启 AstrBot
3. 在后台面板给需要 Key 的插件填上 DeepSeek API Key
4. 想启用主动联系的话，在会话里发一次 `/proactive on`

数据库（`proactive.db` / `memory.db` / `ledger.db`）都是**插件首次启动时自动创建**的，
不需要提前准备，本仓库也不会包含它们。

## 要求

- AstrBot 4.x
- `openai>=1.0`（`proactive_contact` 与 `simple_memory` 会用到）
- 一个 DeepSeek API Key（同上两个插件需要）

## 说明

- 每个插件目录下都有自己的 README，写了配置项、命令和提供给大模型的工具。
- 提示词统一用「用户 / 我」这样的中性称呼，可以配合自己的人格设定使用。
- 表情包插件不附带任何图片，`stickers/` 下的分类目录请自行填充。
