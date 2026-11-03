# 配置参考

配置根键：`private_settings`（旧版 `friend_settings` 会自动迁移）。

所有配置既可以在 AstrBot 插件配置页编辑，也可以在本插件 WebUI 的「配置管理」中编辑。

---

## 1. 顶层结构

```json
{
  "private_settings": { ... },
  "web_admin": { ... },
  "notification_settings": { ... },
  "telemetry_config": { ... }
}
```

本版本没有 `group_settings`。

---

## 2. `private_settings`

### 2.1 基础

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 私聊主动消息总开关 |
| `session_list` | list | `[]` | 私聊 UMO 白名单，只有列表中的会话会被处理 |
| `session_name` | string | - | 会话级覆写才使用的备注名（可选） |

UMO 示例：

```text
qq_official:FriendMessage:0A1B2C3D4E5F60718293A4B5C6D7E8F9
```

### 2.2 `auto_trigger_settings`

插件启动后，如果某个私聊长时间没有消息，可以自动排一次主动消息。

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable_auto_trigger` | bool | `false` | 启用自动触发 |
| `auto_trigger_after_minutes` | int | `5` | 启动后无消息多久触发检查 |

注意：自动触发仍受 `private_settings.enable`、会话白名单、免打扰、未回复暂停与互动强度约束。

### 2.3 `proactive_prompt`

主动消息的主提示词。支持占位符：

| 占位符 | 含义 |
| --- | --- |
| `{{current_time}}` | 当前时间 |
| `{{unanswered_count}}` | 连续未回复次数 |
| `{{intensity_score}}` | 互动强度分 |
| `{{intensity_tier}}` | 强度等级 |
| `{{intensity_probability}}` | 本轮触发概率 |

### 2.4 `context_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `source_mode` | string | `conversation_history` | `conversation_history` / `platform_message_history` / `hybrid` |
| `platform_history_count` | int | `20` | 平台流水条数，建议 10–50 |
| `platform_history_prompt` | text | 内置 | 平台流水注入模板 |
| `include_bot_messages` | bool | `true` | 是否包含 Bot 自己的平台发言 |
| `bot_identifiers` | string | `bot` | 识别 Bot 消息的标识，逗号分隔 |
| `platform_context_max_chars` | int | `4000` | 平台流水上下文最大字符数 |

上下文模式：

- **conversation_history**：使用 AstrBot 当前会话的 LLM 对话历史；
- **platform_message_history**：使用平台最近真实聊天流水，不依赖对话历史；
- **hybrid**：两者都用，平台流水作为额外事实参考。

平台流水读取失败时会自动回退到对话历史。

### 2.5 `schedule_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `min_interval_minutes` | int | `30` | 基础最小间隔 |
| `max_interval_minutes` | int | `600` | 基础最大间隔 |
| `quiet_hours` | string | `1-7` | 免打扰时段，格式 `开始-结束`（24 小时制） |
| `max_unanswered_times` | int | `4` | 连续未回复暂停阈值；达到后停止排期，直到用户再次发来消息；`0` 不限制 |

真实间隔 = 基础区间 × 互动强度缩放，且不低于 `intensity_settings.min_interval_floor_minutes`。

### 2.6 `tts_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable_tts` | bool | `true` | 是否尝试生成语音 |
| `always_send_text` | bool | `true` | 语音发送后是否附带文本 |

### 2.7 `segmented_reply_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `false` | 启用分段回复 |
| `words_count_threshold` | int | `80` | 超过该字数不分段 |
| `split_mode` | string | `regex` | `regex` / `words` |
| `regex` | string | `.*?[。？！~…\n]+|.+$` | 正则分段 |
| `split_words` | list | `['。','？','！','~','…']` | 分词列表 |
| `enable_content_cleanup` | bool | `false` | 分段内容清理 |
| `content_cleanup_rule` | string | `[\n]` | 清理正则 |
| `interval_method` | string | `log` | `log` / `random` |
| `interval` | string | `1.5, 3.5` | random 模式的秒数区间 |
| `log_base` | string | `1.8` | log 模式底数 |

### 2.8 `intensity_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | ---: | --- |
| `enable` | bool | `true` | 启用互动强度调节 |
| `probability_min` | int | `15` | 强度 0 分时的触发概率（%） |
| `probability_max` | int | `95` | 强度 100 分时的触发概率（%） |
| `probability_gamma` | int | `115` | 概率曲线陡峭度（%，100=线性） |
| `interval_scale_hot` | int | `35` | 强度 100 分时间隔缩放（%） |
| `interval_scale_cold` | int | `115` | 强度 0 分时间隔缩放（%） |
| `interval_gamma` | int | `100` | 间隔曲线陡峭度（%） |
| `min_interval_floor_minutes` | int | `8` | 最小间隔下限 |
| `response_window_hours` | int | `6` | 回复统计窗口 |
| `recency_half_life_hours` | int | `36` | 热度半衰期 |
| `apply_to_auto_trigger` | bool | `true` | 自动触发是否也走强度 |
| `prompt_context` | bool | `true` | 是否把强度写入提示词 |
| `skip_retry_ratio` | int | `20` | 概率未命中后的重试比例（%） |
| `min_skip_retry_minutes` | int | `5` | 最短重试等待 |

计算细节见 [intensity.md](intensity.md)。

### 2.9 `pause_settings`

连续未回复暂停后的恢复策略。暂停阈值仍由 `schedule_settings.max_unanswered_times` 控制。

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `resume_mode` | string | `llm_topic` | `llm_topic`：用户新消息先经一次 LLM 话题判定；`any_message`：任何消息都恢复 |
| `topic_judge_prompt` | text | 内置 | 判定提示词，支持 `{{message}}`、`{{history}}`、`{{unanswered_count}}` |
| `topic_judge_timeout_seconds` | int | `10` | 判定超时，2–60 秒 |
| `topic_judge_context_messages` | int | `8` | 注入最近多少条对话历史；0 表示只看新消息 |
| `fallback_resume_on_error` | bool | `true` | 判定超时/报错/无法解析时是否默认恢复 |
| `topic_judge_provider_id` | string | `""` | 可选的专用 Provider ID；留空使用当前聊天 Provider |

`llm_topic` 的判定标准：

- 新话题、问候、分享新事件、明确想继续聊天 → 恢复；
- 继续旧话题、抱怨 Bot 不回、无意义单字/表情 → 保持暂停；
- 图片/语音等非文本消息 → 直接视为重新互动并恢复。

判定结果会写入会话数据的 `last_topic_judge`，管理端「未回复暂停」列表会显示最近一次判定结果。

### 2.10 `humanization_settings`

拟人化层总配置，包含 7 个子项。完整原理与调参建议见 [humanization.md](humanization.md)。

#### `rhythm_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 总开关 |
| `learn_active_hours` | bool | `true` | 从真实消息学习 24 小时活跃分布 |
| `min_messages_to_learn` | int | `10` | 学习所需最少消息数 |
| `active_hours_margin_minutes` | int | `45` | 活跃小时前后放宽 |
| `weekend_margin_minutes` | int | `60` | 周末额外放宽 |
| `daily_proactive_limit` | int | `6` | 每日主动消息上限；0 为不限 |
| `min_gap_minutes` | int | `30` | 两次主动消息最小间隔 |
| `circadian_strength` | float | `0.5` | 昼夜概率系数强度，0–1 |

#### `state_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 关系状态机总开关 |
| `open_thread_hours` | float | `24` | 用户最后消息在多久内视为话题未结束 |
| `awaiting_reply_hours` | float | `36` | 主动消息发出后多久内视为等待回应 |
| `cooling_hours` | float | `72` | 降温状态持续时长 |
| `silent_intent_enabled` | bool | `true` | 是否允许“暂不发言” |
| `silent_max_streak` | int | `2` | 连续沉默上限，达到后强制发言 |

#### `feedback_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 启用回复质量识别 |
| `negative_cooldown_hours` | float | `24` | 负反馈冷却时长；冷却期内即使重新开启话题也不会立即主动 |
| `perfunctory_no_reset` | bool | `true` | 敷衍回复是否保留未回复计数 |
| `min_substantive_length` | int | `6` | 实质回复最短字数参考 |

#### `guard_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 启用发送前守卫 |
| `repeat_similarity` | float | `0.65` | 与最近主动消息的相似度阈值 |
| `recent_message_count` | int | `5` | 对比最近多少条主动消息 |
| `max_message_chars` | int | `300` | 单条主动消息最大字数 |
| `max_consecutive_questions` | int | `2` | 连续提问上限 |
| `blocked_patterns` | text | `""` | 自定义禁用正则 |
| `rewrite_on_fail` | bool | `true` | 命中后是否调用一次轻量 LLM 改写 |

#### `delivery_plan_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 启用气泡发送计划 |
| `max_bubbles` | int | `3` | 最多气泡数 |
| `bubble_length_threshold` | int | `60` | 超过该字数才拆分 |
| `first_delay_min_seconds` | float | `1` | 首条气泡最小延迟 |
| `first_delay_max_seconds` | float | `4` | 首条气泡最大延迟 |
| `inter_bubble_min_seconds` | float | `1` | 气泡间最小延迟 |
| `inter_bubble_max_seconds` | float | `3` | 气泡间最大延迟 |
| `voice_probability` | float | `0.25` | 语音发送概率，0–1 |
| `max_total_delay_seconds` | float | `30` | 总延迟上限 |

#### `persona_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 启用心情/精力状态 |
| `baseline_energy` | float | `0.6` | 基础精力，0–1 |
| `low_energy_threshold` | float | `0.35` | 低精力阈值 |
| `low_energy_probability_reduction` | float | `0.3` | 低精力时最多降低多少概率 |

#### `memory_integration_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 启用长期记忆接入 |
| `plugin_id` | string | `astrbot_plugin_livingmemory` | 记忆插件 ID |
| `top_k` | int | `5` | 召回条数；0 为不召回 |
| `inject_into_prompt` | bool | `true` | 是否注入提示词 |
| `write_proactive` | bool | `true` | 是否写回主动消息 |
| `timeout_seconds` | float | `5` | 记忆操作超时 |

#### `conversation_memory_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 启用短期工作记忆 |
| `max_open_loops` | int | `8` | 最多保留开放话题 |
| `max_recent_topics` | int | `8` | 最多保留最近话题 |
| `ttl_days` | float | `7` | 开放话题保留天数 |
| `followup_weight` | float | `1.5` | 有开放话题时的跟进权重 |
| `profile_enabled` | bool | `true` | 维护轻量表达画像 |

#### `generation_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `planner_enabled` | bool | `false` | 启用 planner |
| `critic_enabled` | bool | `false` | 启用 critic |
| `planner_timeout_seconds` | float | `8` | planner 超时 |
| `critic_timeout_seconds` | float | `10` | critic 超时 |
| `planner_provider_id` | string | `""` | planner 专用 Provider |
| `critic_provider_id` | string | `""` | critic 专用 Provider |

---

## 3. `web_admin`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 启用独立 WebUI |
| `host` | string | `127.0.0.1` | 监听地址 |
| `port` | int | `4100` | 监听端口 |
| `password` | string | `""` | 访问密码，留空则无需登录 |

生产环境建议设置强密码，并只监听内网/本机。

> `web_admin` 只控制**独立 WebUI**。AstrBot 原生插件页面（`views/proactive-chat`）由 Dashboard 直接加载，不受 `enabled`、端口或密码影响。如果只用 AstrBot 页面，可以把 `web_admin.enabled` 设为 `false` 以减少一个监听端口。

---

## 4. `notification_settings`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 启用远端通知同步 |
| `poll_interval_seconds` | int | `300` | 轮询间隔，最小按 30 秒处理 |

---

## 5. `telemetry_config`

| 字段 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 匿名遥测 |

遥测不会上传会话列表原文、提示词正文、聊天内容或管理端密码。

---

## 6. 会话差异配置

WebUI 的「配置管理 → 会话差异配置」可以为单个私聊覆写：

- `enable`
- `session_name`
- `proactive_prompt`
- `context_settings`
- `auto_trigger_settings`
- `schedule_settings`
- `intensity_settings`
- `tts_settings`
- `segmented_reply_settings`

覆写保存在插件数据目录的 `session_overrides.json`。清空覆写即恢复全局配置。

---

## 7. 配置迁移

- 启动时若发现 `friend_settings` 且没有 `private_settings`，会自动复制一份；
- 不会删除 `friend_settings`，便于回滚；
- `group_settings` 不再读取，可手动删除；
- 旧 `session_data.json` 中的群聊键会在启动恢复时跳过并清理调度字段。
