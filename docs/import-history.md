# 从 AstrBot 数据库导入历史强度

插件运行时只统计**启用插件之后**收到的私聊消息。新装插件时，老用户的强度会从 0 开始。

如果你希望一开始就有比较准确的强度，可以把 AstrBot 数据库里的历史私聊导入为“强度种子”。

> 工具只读取 `FriendMessage` / `PrivateMessage` 会话，不会读取群聊，不会修改数据库。
> 写入前会自动备份 `session_data.json`。

---

## 1. 先出报告

```bash
python tools/import_astrbot_history.py \
  --db /path/to/AstrBot/data/data_v4.db \
  --report ./intensity_report.json
```

只读本地数据，输出：

- 每会话的当前强度、峰值强度、等级、概率、有效间隔；
- 24h / 7d 消息量、活跃天、中位间隔；
- 汇总：会话数、消息数、平均强度、等级分布。

建议先看报告，确认分数合理，再写入。

### 常用参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--db` | 必填 | `data_v4.db` 路径 |
| `--platform` | 全部 | 只导入某个平台前缀，如 `qq_official` |
| `--timezone` | `Asia/Shanghai` | 数据库时间里所在的时区 |
| `--report` | `intensity_report.json` | 报告路径；传空字符串则不写 |
| `--data-dir` | - | 插件数据目录 |
| `--write` | 关闭 | 写入 `session_data.json` |
| `--min-messages` | `1` | 只处理至少 N 条用户消息的会话 |
| `--now` | 当前时间 | 评分基准时间（ISO） |
| `--probability-min` / `--probability-max` | 15 / 95 | 报告中使用的概率参数 |

---

## 2. 写入种子

```bash
python tools/import_astrbot_history.py \
  --db /path/to/AstrBot/data/data_v4.db \
  --data-dir /path/to/AstrBot/data/plugin_data/astrbot_plugin_proactive_chat \
  --min-messages 20 \
  --write
```

写入逻辑：

1. 读取 `session_data.json`；
2. 备份为 `session_data.json.bak`；
3. 为每个会话写入/覆盖 `intensity` 字段；
4. 保留原有的 `unanswered_count`、`last_message_time`、`next_trigger_time` 等字段；
5. 原子替换文件。

建议 `--min-messages 20` 或更高，只给真正聊过的用户做种子，避免一次写入几百个一次性会话。

---

## 3. 数据目录在哪

常见位置：

```text
AstrBot/data/plugin_data/astrbot_plugin_proactive_chat/
```

里面应该有：

```text
session_data.json
session_overrides.json
```

也可以在 WebUI「运行状态」页打开插件数据目录（`/api/open-directory` 的 `path=data`）。

---

## 4. 时间从哪里来

AstrBot 的 `conversations.content` 里，用户消息通常带有：

```text
<system_reminder>Current datetime: 2026-10-27 13:23 (CST), Weekday: Tuesday</system_reminder>
```

工具会：

- 从中提取 `YYYY-MM-DD HH:MM`；
- 按 `--timezone` 转成 epoch；
- 没有时间戳的用户消息会被跳过（无法放置到时间线）；
- 同一会话内容重复出现时按「时间 + 文本前缀」去重。

---

## 5. 导入后的行为

种子只提供“历史行为”：

- `events`：最近 200 条用户消息特征；
- `daily`：最近 45 天每日消息数；
- `total_user_messages`、`first_seen`、`last_user_ts`；
- `seed.source = "astrbot_db"`。

运行时：

- 用户发新消息 → 继续累加；
- 主动消息发送/回复 → 继续记录；
- 新鲜度会随时间衰减，所以导入的历史不会永远保持高热度；
- WebUI 会显示「已从数据库导入」标记。

`proactive` 历史为空：数据库里没有“哪条是主动消息”的可靠标记，因此不伪造回复率。回复率仍从本插件运行后开始统计。

---

## 6. 报告怎么读

```text
 #     当前     峰值  峰值等级    概率  间隔(分钟)       24h    7d  活跃天  中位间隔  会话
 1    9.0   76.0  炽热     73%   16.3-325.4      0     1     7      1.0  qq_official:FriendMessage:B00...
```

- **当前**：按 `--now` 评分的当前强度（历史用户通常因为沉默而较低）；
- **峰值**：按该会话最后一条用户消息时刻评分，代表活跃期强度；
- **概率 / 间隔**：峰值时的概率与有效间隔；
- **24h / 7d**：当前时间窗口内的消息量（历史用户可能是 0）。

判断标准：

- 峰值炽热/亲密 → 长期高频用户，导入后更容易被主动找；
- 峰值平稳/冷淡 → 偶尔聊天，导入后仍会保持较低概率；
- `当前` 很低是正常的，因为新鲜度已经衰减。

---

## 7. `--platform` 与历史拼写

真实数据库里可能出现：

```text
qq_offficial
qqofficial
qq_official
```

它们可能是同一平台不同时期的名字。工具默认导入全部私聊；如果只想要当前平台：

```bash
--platform qq_official
```

但历史 UMO 的平台段不会自动改写。如果旧 UMO 与当前平台 ID 不一致，导入的种子会挂在旧 UMO 下，运行时不会命中。此时：

1. 先看 AstrBot 当前日志里的平台 ID；
2. 用 `--platform` 过滤；
3. 必要时手工把 `session_data.json` 中的旧 UMO 键改成当前 UMO。

---

## 8. 安全与隐私

- 数据库以 `mode=ro` 只读打开；
- 工具不联网；
- 报告包含 UMO 与统计值，**不包含聊天正文**；
- 写入文件只包含事件时间、长度、疑问/感叹/表情标记，不保存原文；
- 报告文件请勿随意公开，UMO 可能包含用户标识。
