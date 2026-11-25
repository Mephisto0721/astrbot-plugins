# astrbot_plugin_sticker — 表情包

让机器人在合适的时候主动发一张表情，而不是永远只发文字。

图片按情绪分类存放，需要时从对应分类随机抽一张发出去。

## 依赖

- AstrBot 4.x
- 无需额外 Python 包

## 目录结构

```
astrbot_plugin_sticker/
└── stickers/
    ├── happy/    开心
    ├── shy/      害羞
    ├── angry/    生气
    ├── sad/      难过
    └── smug/     得意
```

把图片（png / jpg / gif / webp）按情绪丢进对应目录即可，不需要改代码。

## 工作方式

- 模型可以主动调用 `send_sticker` 工具，指定情绪发一张
- 有冷却：默认两次发送之间至少隔 5 轮对话，避免刷屏
- 分类里没有图时不会硬发，会返回提示

## 配置

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 是否启用 |
| `cooldown_turns` | int | 5 | 两次发送之间至少隔几轮 |

## 命令

```
/sticker list        查看各情绪分类各有多少张
/sticker <情绪>      手动发一张
/sticker help        查看用法
```

情绪可选：`happy`、`shy`、`angry`、`sad`、`smug`。

## 提供给大模型的工具

| 工具 | 作用 |
|---|---|
| `send_sticker` | 按情绪发一张表情 |

## 数据

图片在 `stickers/` 下按情绪分类存放。本插件不带图片资源，需要自己准备。
