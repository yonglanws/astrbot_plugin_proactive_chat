# 拟人化：作息、状态、自然度与长期记忆

本页说明 v2.3.0 新增的拟人化层。目标不是让 Bot“假装成真人”，而是让主动消息在**时间、状态、节奏和反馈**上更自然，同时保留明确的频率上限与退订边界。

---

## 1. 设计原则

1. **先决定说不说，再决定说什么**：不是每次到点都必须发消息。
2. **时间要像人的作息**：在对方通常活跃的时段出现，而不是均匀随机。
3. **关系有状态**：话题未结束、等待回应、降温中、暂停，行为不同。
4. **回复有质量**：敷衍、负反馈和实质回复不能一视同仁。
5. **发送前做一次自然度检查**：重复、过长、连续提问、客服腔要拦下来。
6. **表达有节奏**：允许 1–3 条气泡、允许短句、允许偶尔发语音。
7. **复用已有长期记忆**：不重复造轮子，通过 LivingMemory 召回与写回。

---

## 2. 作息节律（`rhythm_settings`）

### 学什么

`RhythmService` 从真实用户消息中统计 24 小时的活跃分布，并做按天衰减：

```text
hour_counts[24]   每个小时的活跃次数（指数衰减）
total_activity    累计样本数
daily_proactive   每日已发送的主动消息数
last_user_ts      最近用户消息时间
last_proactive_ts 最近主动消息时间
```

样本不足 `min_messages_to_learn` 时，不启用学习结果，只使用固定免打扰时段。

### 调度时怎么用

候选触发时间会依次经过：

```text
最小间隔检查
→ 每日主动消息预算
→ 学习到的活跃窗口（含前后 margin）
→ 周末额外放宽
→ 固定免打扰时段兜底
```

如果候选时间落在窗口外，会顺延到下一个可用时间，而不是直接发送。

### 昼夜概率系数

调度窗口之外，还会按当前小时与活跃时段的距离给出一个温和的概率系数：

```text
distance = 当前小时到最近活跃小时的环形距离
raw = max(0.35, 1 - distance × 0.12)
multiplier = 1 - circadian_strength × (1 - raw)
```

`circadian_strength=0` 时不参与，`1` 时完全按活跃时段加权。

### 关键配置

| 字段 | 默认 | 说明 |
|---|---|---|
| `enable` | `true` | 总开关 |
| `learn_active_hours` | `true` | 是否学习活跃时段 |
| `min_messages_to_learn` | `10` | 学习所需最少消息数 |
| `active_hours_margin_minutes` | `45` | 活跃小时前后放宽分钟数 |
| `weekend_margin_minutes` | `60` | 周末额外放宽 |
| `daily_proactive_limit` | `6` | 每日主动消息上限；`0` 为不限 |
| `min_gap_minutes` | `30` | 两次主动消息最小间隔 |
| `circadian_strength` | `0.5` | 昼夜概率系数强度 |

---

## 3. 会话关系状态（`state_settings`）

状态持久化在 `session_data[umo]["conversation_state"]`。

| 状态 | 含义 | 典型行为 |
|---|---|---|
| `idle` | 很久没有有效互动 | 更倾向开启新话题或分享自己 |
| `open_thread` | 最近聊过，话题可能还没结束 | 优先延续具体话题 |
| `awaiting_reply` | Bot 刚主动发过，对方还没回 | 更克制，可能选择暂不发言 |
| `cooling` | 对方回复变短、变慢或出现负反馈 | 轻量关心，降低频率 |
| `paused` | 连续未回复或负反馈暂停 | 不再主动发送 |

### 判断规则

- `paused`：存在 `proactive_paused` 标记。
- `awaiting_reply`：最近一条主动消息晚于用户消息，未回复计数 > 0，且在 `awaiting_reply_hours` 内。
- `open_thread`：用户最后一条消息在 `open_thread_hours` 内。
- `cooling`：超过等待窗口，或最后回复质量为敷衍/负反馈。
- `idle`：超过 `cooling_hours` 没有有效互动。

状态会随时写入提示词，让模型知道“现在是什么关系阶段”。

---

## 4. 主动意图（INTENT 阶段）

在生成文本之前，`IntentStage` 先选择一个意图：

| 意图 | 说明 |
|---|---|
| `silent` | 本轮不发言，直接重排期 |
| `continue_topic` | 延续上次没聊完的具体话题 |
| `new_topic` | 开启轻量新话题 |
| `care` | 关心对方最近提到的事 |
| `share_self` | 分享自己此刻的一件小事 |
| `ask` | 只问一个具体问题 |

权重由关系状态、人格精力、历史反馈共同决定。例如：

- `open_thread`：`continue_topic` 权重最高；
- `awaiting_reply`：未回复次数越多，`silent` 权重越高；
- `cooling`：`care` 权重最高，但整体更克制；
- 低精力：降低分享与提问，增加沉默；
- 连续沉默达到 `silent_max_streak` 后强制发言，避免一直不出现。

手动触发（WebUI「立即触发」）永远不会选择 `silent`。

---

## 5. 回复质量与反馈（`feedback_settings`）

`classify_reply()` 把用户回复分为：

| 质量 | 例子 | 行为 |
|---|---|---|
| `substantive` | “刚看完那部电影，结尾有点意外” | 视为重新投入，未回复计数清零 |
| `question` | “你明天有空吗？” | 同上，并提高 engagement |
| `perfunctory` | “嗯”“哦”“好”“😂” | **不清零**未回复计数，且不结算上一条主动消息为已回复 |
| `negative` | “别发了”“在忙”“不要打扰” | **暂停**主动消息并进入冷却；同样不结算为已回复 |
| `media` | 图片/语音/表情包 | 视为互动，清零计数 |

反馈统计按“小时桶 × 意图”记录：

```text
feedback_stats[hour_bucket][intent] = {
  "positive": 0,
  "neutral": 0,
  "negative": 0
}
```

意图选择时使用 Beta 平滑后的正向率加权，小样本不会产生极端偏好。

### 负反馈

明确负反馈会写入 `proactive_paused`，暂停原因记为 `negative_feedback`，并写入 `cooldown_until`。冷却期内即使用户重新开启话题并解除暂停，也不会立刻主动发送；管理端手动「恢复」会清除冷却。管理端暂停列表会显示“用户明确表示不想收到主动消息”和剩余冷却时间。

---

## 6. 发送前自然度守卫（`guard_settings`）

GUARD 阶段在发送前检查：

- **重复**：与最近 N 条主动消息的字符序列/二元组相似度超过阈值；
- **过长**：超过 `max_message_chars`；
- **连续提问**：最近几条主动消息都在提问，本轮又是问题；
- **AI 自指/索取情绪价值**：如“作为 AI”“怎么还不回我”“是不是讨厌我”；
- **客服腔**：如“有什么我可以帮你”“非常抱歉”；
- **自定义正则**：`blocked_patterns`。

命中后：

1. `rewrite_on_fail=true`：调用一次轻量 LLM 改写，保留人格与原意；
2. 改写后仍不合格，或关闭改写：放弃本轮并重新调度；
3. 结果写入 `last_guard`，管理端可追溯。

纯富媒体消息（图片/语音）没有文本可校验，会直接放行。

---

## 7. 气泡发送计划（`delivery_plan_settings`）

`DeliveryPlanStage` 把一段回复变成 1–3 条气泡：

```json
{
  "bubbles": [
    {"text": "刚下班，路过那家店", "delay_seconds": 2.4},
    {"text": "突然想起你上次说想吃", "delay_seconds": 1.8}
  ],
  "voice": false,
  "ask_question": true
}
```

规则：

- 文本不超过 `bubble_length_threshold`：只发一条；
- 超过：按句号/问号/感叹号/省略号切分，再按长度合并；
- 每条气泡有独立延迟，首条模拟“想一下”，后续模拟打字；
- 总延迟受 `max_total_delay_seconds` 限制；
- 语音只在 `voice_probability` 命中时尝试，不再“启用 TTS 就每条都发”；
- 关闭发送计划后，回退到原有的正则分段设置。

---

## 8. 人格心情与精力（`persona_settings`）

`PersonaStateService` 维护每个会话的轻量状态：

```json
{
  "energy": 0.65,
  "mood": "calm",
  "activity": "在忙，但偶尔会看手机"
}
```

- 精力随时段起伏（深夜低、下午高），并以 6 小时半衰期回归基线；
- 连续未回复、负反馈会降低精力；实质回复会略微提升；
- 低精力时降低主动概率、缩短表达；
- 状态会写入提示词，例如“你此刻有点累，精力 42%，在忙，但偶尔会看手机”。

它不是复杂的情绪模拟，也不会编造重大生活事件。

---

## 8.5 短期工作记忆：画像、开放话题与话题账本（`conversation_memory_settings`）

长期事实记忆交给 LivingMemory；本插件只维护“最近聊到哪、有什么还没闭环”的短期工作记忆。

### 三类状态

| 部分 | 内容 | 用途 |
|---|---|---|
| `profile` | 平均消息长度、提问比例、表情比例、最后回复质量 | 调整表达长度与风格 |
| `open_loops` | 用户提到的计划/事件、助手问过但未回答的问题 | 主动消息时自然跟进，避免重新查户口 |
| `recent_topics` | 最近几条用户/助手话题及意图 | 延续话题、去重 |

### 开放话题怎么产生

- 用户消息里出现“明天/下周/等会/回头”等时间词，或“面试/考试/上班/医院”等事件词 → 建立 `plan` / `event` 话题；
- 助手发出带问号的消息 → 建立 `question` 话题；
- 用户后续消息命中话题关键词，或给出实质回复 → 关闭对应话题；
- 超过 `ttl_days` 仍未闭环 → 自动关闭。

### 提示词注入

有开放话题时，会注入：

```text
<topic_ledger>
以下是你和对方最近还没有闭环的话题。可以自然跟进其中一条，
但不要逐条追问，也不要表现得像在核对清单：
- [计划] 对方明天下午有面试，有点紧张
- [未回答的问题] 你之前问过：准备得怎么样了？
</topic_ledger>
```

同时意图选择会提高 `continue_topic` / `care` 的权重。

### 配置

| 字段 | 默认 | 说明 |
|---|---|---|
| `enable` | `true` | 总开关 |
| `max_open_loops` | `8` | 最多保留开放话题 |
| `max_recent_topics` | `8` | 最多保留最近话题 |
| `ttl_days` | `7` | 开放话题保留天数 |
| `followup_weight` | `1.5` | 有开放话题时的跟进权重 |
| `profile_enabled` | `true` | 是否维护轻量表达画像 |

---

## 8.6 多阶段生成：planner / writer / critic（`generation_settings`）

默认仍是单阶段生成（writer），避免常规主动消息多付 LLM 成本。需要更高质量时可以开启：

```text
PLAN（planner，可选）
  → DELIBERATE（writer，现有 LLM 调用）
  → GUARD（规则守卫）
  → CRITIC（critic，可选）
```

- **planner**：写作前生成 `{topic, goal, tone, avoid}`，注入提示词；
- **writer**：现有的 DELIBERATE 阶段；
- **critic**：发送前做一次语义级审稿，返回 `pass/reason/rewrite`；不通过且无改写时放弃本轮并重排期；
- 任一阶段超时/失败都会自动降级，不影响主流程。

| 字段 | 默认 | 说明 |
|---|---|---|
| `planner_enabled` | `false` | 启用 planner |
| `critic_enabled` | `false` | 启用 critic |
| `planner_timeout_seconds` | `8` | planner 超时 |
| `critic_timeout_seconds` | `10` | critic 超时 |
| `planner_provider_id` | `""` | planner 专用 Provider |
| `critic_provider_id` | `""` | critic 专用 Provider |

---

## 9. 长期记忆接入（`memory_integration_settings`）

本插件**不实现自己的长期记忆**，而是复用已安装的 [LivingMemory](../../astrbot_plugin_livingmemory)：

- 生成主动消息前：用最近几条对话 + 当前关系状态/意图构造查询，调用记忆插件的 `search_memories`；
- 召回结果以 `<long_term_memory>` 临时块注入提示词；
- 主动消息发送成功后：通过记忆插件的 `conversation_manager.add_message` 写回一条 assistant 消息；
- 未安装、未初始化或接口不兼容时自动降级，不影响主流程。

### 作用域兼容

桥接层会读取 LivingMemory 的过滤配置：

| `memory_scope_mode` | 召回作用域 |
|---|---|
| `session` | 当前 UMO |
| `global` | `livingmemory:global` |
| `user` | `livingmemory:user:{platform}:{target_id}` |
| `legacy` | 按 `use_session_filtering` 决定 UMO 或全局 |

`use_persona_filtering=false` 时不传 `persona_id`，避免错误过滤。

### 写回

主动消息不经过 AstrBot 的 `on_llm_response` 钩子，因此需要显式写回：

```python
await conversation_manager.add_message(
    session_id=umo,
    role="assistant",
    content=text,
    is_bot_message=True,
    metadata={"source": "proactive_chat"},
)
```

重复内容会跳过，避免重试导致重复记忆。

### 配置

| 字段 | 默认 | 说明 |
|---|---|---|
| `enable` | `true` | 总开关 |
| `plugin_id` | `astrbot_plugin_livingmemory` | 记忆插件 ID |
| `top_k` | `5` | 召回条数；`0` 为不召回 |
| `inject_into_prompt` | `true` | 是否注入提示词 |
| `write_proactive` | `true` | 是否写回主动消息 |
| `timeout_seconds` | `5` | 记忆操作超时 |

> 本插件只以只读方式了解 LivingMemory 的公开接口，不修改其代码或数据。

---

## 10. 状态字段

新增的 `session_data` 字段：

```json
{
  "rhythm": {
    "hour_counts": [0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 5, 2, 0, 0, 0, 0, 0, 0, 0, 4, 6, 3, 1, 0],
    "daily_proactive": {"2026-11-02": 2},
    "last_proactive_ts": 1793632472.0
  },
  "conversation_state": {
    "state": "awaiting_reply",
    "last_user_quality": "substantive",
    "last_intent": "care",
    "feedback_stats": {}
  },
  "persona_state": {
    "energy": 0.65,
    "mood": "calm",
    "activity": "在放松"
  },
  "conversation_memory": {
    "profile": {"message_count": 12, "avg_length": 18.5},
    "open_loops": [{"kind": "plan", "text": "明天下午有面试"}],
    "recent_topics": []
  },
  "cooldown_until": 1793632472.0,
  "last_proactive_texts": ["..."],
  "last_reply_quality": "substantive",
  "last_guard": {"allowed": true, "passed": true}
}
```

这些字段都参与 `SessionStore.merge_session_info` 的合并，避免多键/旧键同步时丢失。

---

## 11. 建议的调参顺序

1. 先看「互动强度」页，确认强度分与概率合理；
2. 再看「运行状态」页的暂停原因与回复质量；
3. 如果主动消息太频繁：
   - 降低 `daily_proactive_limit`；
   - 提高 `min_gap_minutes`；
   - 提高 `probability_gamma`；
4. 如果总在奇怪的时间发：
   - 确认 `quiet_hours`；
   - 检查「活跃时段」是否已学习；
   - 提高 `active_hours_margin_minutes`；
5. 如果内容重复或太像客服：
   - 降低 `repeat_similarity`（更严格）；
   - 打开 `rewrite_on_fail`；
   - 检查 `max_message_chars` 与 `max_consecutive_questions`；
6. 如果用户明确反感：
   - 负反馈会自动暂停；
   - 不要通过缩短 `negative_cooldown_hours` 来绕过用户边界。

---

## 12. 安全边界

- 每日上限与最小间隔始终优先于强度；
- 手动触发也不会绕过“暂停”状态；
- 负反馈暂停后，只有用户重新开启话题或管理端手动恢复才会继续；
- 不模拟内疚、威胁、情感绑架；
- 不伪造与用户有关的事实；
- 长期记忆只作为事实参考，不向用户暴露“记忆库”概念。

---

## 13. 离线回放

`tools/replay_humanization.py` 可以用 AstrBot 数据库中的私聊时间线离线评估策略：

```bash
python tools/replay_humanization.py \
  --db /path/to/data_v4.db \
  --report ./replay_report.json
```

输出：

- 每个会话的主动尝试次数、实际发送、概率未命中、暂停次数；
- 负反馈 / 敷衍 / 实质回复数量；
- 回复率（敷衍与负反馈不会结算为上一条主动消息的“已回复”）；
- 发送时段分布，便于检查作息是否合理。

数据库以只读模式打开，不会修改原文件。可用 `--settings` 传入自定义 JSON 配置做策略对比。

---

## 14. 测试

新增测试覆盖：

- 回复质量分类、文本相似度、发送计划；
- 作息学习、活跃窗口、每日预算、最小间隔、免打扰；
- 关系状态转移、意图权重、反馈统计、连续沉默上限；
- 自然度守卫的重复/长度/提问/AI 腔与一次改写；
- 人格精力曲线与低精力概率降幅；
- LivingMemory 召回、作用域、格式化与写回去重；
- 完整流水线集成（GATE → INTENT → CONTEXT → DELIBERATE → REVIEW → GUARD → DELIVERY_PLAN → DELIVER → RECORD）。

运行：

```bash
python -m pytest -q
```
