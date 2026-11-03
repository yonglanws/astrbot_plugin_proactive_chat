# 互动强度模型

本文说明「互动强度」如何从真实私聊行为中计算，以及它如何影响主动消息的触发概率与间隔。

实现代码：

- 纯模型：`core/domain/intensity.py`
- 状态与决策：`core/services/intensity_service.py`

强度只决定“基础概率与间隔”。v2.3.0 起，实际发送还会经过作息节律、关系状态、人格精力、回复质量与自然度守卫，详见 [humanization.md](humanization.md)。

---

## 1. 一句话版本

> 强度分 = 5 个行为信号的加权和 × 新鲜度 × 样本置信度；分数越高，主动消息越容易触发，间隔越短。

---

## 2. 输入信号

| 信号 | 权重 | 计算方式 | 直觉 |
| --- | ---: | --- | --- |
| `frequency` 聊天频率 | 0.30 | 24 小时消息量（饱和值 40）与 7 天日均（饱和值 25）的对数饱和，6:4 加权 | 最近发了多少条 |
| `tempo` 回复节奏 | 0.22 | `exp(-中位间隔分钟 / 120)`；无间隔数据时取 0.25 | 来回有多快 |
| `streak` 连续活跃 | 0.16 | 最近 14 天有消息的天数 / 10，封顶 1 | 是不是持续在聊 |
| `engagement` 主动回复意愿 | 0.18 | 回复率 ×（0.65 + 0.35 × 回复速度因子）；无历史时取 0.4 | 对主动消息买不买账 |
| `expressiveness` 表达欲 | 0.14 | 0.40×长度分 + 0.25×疑问率 + 0.15×感叹率 + 0.20×表情率 | 聊得投入不投入 |

### 回复速度因子

```text
latency_factor = clamp(exp(-(平均回复分钟 - 5) / 120), 0.15, 1.0)
engagement = clamp(reply_rate × (0.65 + 0.35 × latency_factor), 0, 1)
```

5 分钟内回复接近 1.0；2 小时左右约 0.4；更慢时降到 0.15 下限。

### 长度分

```text
length_score = clamp(log(1 + 平均字数) / log(1 + 120), 0, 1)
```

平均 120 字左右达到饱和。

---

## 3. 修正项

### 新鲜度

```text
recency = 0.5 ^ (距离上一条用户消息的小时数 / recency_half_life_hours)
freshness = 0.15 + 0.85 × recency
```

默认半衰期 36 小时：

- 刚聊完：`freshness ≈ 1`
- 沉默 36 小时：`freshness ≈ 0.575`
- 沉默 7 天：`freshness ≈ 0.18`

一个曾经 80 分、但一周没说话的用户，会被压到 15 分左右，避免“诈尸式主动”。

### 样本置信度

```text
volume = 0.55 + 0.45 × clamp(log(1 + 累计用户消息数) / log(1 + 30), 0, 1)
```

- 1 条消息：约 0.64
- 5 条消息：约 0.79
- 30 条以上：1.0

只有一两条消息的会话不足以判断“聊得热”，会被压低。

### 最终分数

```text
raw   = 0.30×frequency + 0.22×tempo + 0.16×streak + 0.18×engagement + 0.14×expressiveness
score = clamp(100 × raw × freshness × volume, 0, 100)
```

---

## 4. 等级

| 分数 | 等级 key | 中文 |
| ---: | --- | --- |
| 0–19 | `dormant` | 冷淡 |
| 20–39 | `steady` | 平稳 |
| 40–54 | `warm` | 热络 |
| 55–69 | `close` | 亲密 |
| 70–100 | `blazing` | 炽热 |

阈值参考真实私聊数据校准：大多数一次性会话落在 40 以下，持续互动集中在 40–70，长期高频用户在 70 以上。

---

## 5. 概率映射

```text
x = score / 100
p = probability_min + (probability_max - probability_min) × x ^ probability_gamma
```

默认 `probability_min=15%`、`probability_max=95%`、`probability_gamma=1.15`。

| 分数 | 默认概率 |
| ---: | ---: |
| 0 | 15% |
| 25 | 31% |
| 50 | 51% |
| 75 | 72% |
| 100 | 95% |

`probability_gamma > 1` 会让中低强度用户的概率更低、高强度用户更突出。设为 1 即线性。

概率只在**定时/自动触发**时判定；Web 管理端的「立即触发」永远跳过概率。

---

## 6. 间隔映射

```text
scale = interval_scale_cold - (interval_scale_cold - interval_scale_hot) × x ^ interval_gamma
effective_min = max(min_interval_floor_minutes, base_min × scale)
effective_max = max(effective_min, base_max × scale)
```

默认 `interval_scale_cold=1.15`、`interval_scale_hot=0.35`、`interval_gamma=1.0`、`min_interval_floor_minutes=8`。

以基础间隔 30–600 分钟为例：

| 分数 | scale | 有效间隔 |
| ---: | ---: | --- |
| 0 | 1.15 | 34.5–690 分钟 |
| 25 | 0.95 | 28.5–570 分钟 |
| 50 | 0.75 | 22.5–450 分钟 |
| 75 | 0.55 | 16.5–330 分钟 |
| 100 | 0.35 | 10.5–210 分钟 |

**下限优先**：即使强度 100、基础最小间隔 1 分钟，也不会低于 `min_interval_floor_minutes`。

---

## 7. 概率未命中怎么办

不会等待完整的 10 小时，而是：

```text
retry = max(min_skip_retry_minutes, effective_min × skip_retry_ratio)
```

默认 `min_skip_retry_minutes=5`、`skip_retry_ratio=0.2`。

例如有效最小间隔 16.5 分钟 → 重试等待 `max(5, 3.3) = 5` 分钟。高热度用户因此有更多自然触发窗口，同时不会刷屏。

每次判定都会写入 `last_decision`，WebUI 的「互动强度」页可以看到分数、概率、随机值和是否命中。

---

## 8. 状态存储

每个会话的 `session_data[umo]["intensity"]`：

```json
{
  "version": 1,
  "events": [
    {"t": 1760000000.0, "n": 18, "q": 1, "e": 0, "m": 0}
  ],
  "daily": {"2026-11-01": 24, "2026-11-02": 31},
  "proactive": [
    {"ts": 1760000000.0, "replied_at": 1760000120.0, "score": 72.4}
  ],
  "total_user_messages": 312,
  "first_seen": 1759000000.0,
  "last_user_ts": 1760000000.0,
  "last_decision": {
    "at": 1760000300.0,
    "trigger": "schedule",
    "score": 72.4,
    "tier": "blazing",
    "probability": 0.69,
    "roll": 0.42,
    "will_send": true
  },
  "seed": {"source": "astrbot_db", "imported_at": 1760000000.0, "platform": "qq_official"}
}
```

上限：

- `events` 最近 200 条；
- `daily` 最近 45 天；
- `proactive` 最近 30 条 / 30 天。

---

## 9. 回复结算

1. 主动消息成功送达 → `record_proactive_sent` 追加一条待回复记录；
2. 用户在 `response_window_hours` 内发来消息 → 标记 `replied_at`，计入回复率与平均回复时间；
3. 超过窗口仍未回复 → 标记 `missed`，计入“未回复”；
4. 仍在窗口内的记录不参与回复率，避免“刚发出去就算失败”。

只有已启用 `private_settings.enable` 且会话命中 `session_list` 的私聊才会记录强度事件。

---

## 10. 参数怎么调

| 想要的效果 | 调整 |
| --- | --- |
| 整体更主动 | 提高 `probability_min` / `probability_max` |
| 只让热聊用户更主动 | 提高 `probability_gamma` |
| 热聊时间隔更短 | 降低 `interval_scale_hot` |
| 冷淡用户更少被打扰 | 提高 `interval_scale_cold` 或降低 `probability_min` |
| 避免太频繁 | 提高 `min_interval_floor_minutes` |
| 热度衰减更快 | 降低 `recency_half_life_hours` |
| 新用户更快变热 | 降低 `probability_gamma`，或接受 `volume` 的自然压制 |

---

## 11. 真实数据校准记录

离线报告（只读本地数据库，不上传内容）：

- 私聊会话：784
- 用户消息：14,600
- 会话内相邻消息间隔：
  - 中位：2 分钟
  - P75：5 分钟
  - P90：90 分钟
- 按最后一条用户消息时刻评分：

| 等级 | 会话数 |
| --- | ---: |
| 冷淡 | 278 |
| 平稳 | 272 |
| 热络 | 147 |
| 亲密 | 82 |
| 炽热 | 5 |

复现方式见 [import-history.md](import-history.md)。
