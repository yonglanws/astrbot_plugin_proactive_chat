# 更新日志

本项目的版本线从 `v2.0.0` 开始，以“私聊 + 互动强度”为基线。
上游 `v1.x` 的历史日志已不再维护；如需追溯，请查看原仓库。

---

## v2.4.0 — AstrBot 原生插件页面

### 新增

- **AstrBot Page 入口**：新增 `views/proactive-chat/`，在 AstrBot WebUI 的插件详情页即可打开「主动消息管理台」，不再必须访问独立端口。
- **同一份前端，两种运行方式**：
  - AstrBot 内嵌模式：通过 `window.AstrBotPluginView` bridge 调用插件 API，主题、字体、卡片与页签跟随 AstrBot Dashboard；
  - 独立 WebUI：继续使用原有的 HTTP + WebSocket、登录密码与端口配置。
- **页面后端 API**（`core/console/page_api.py`）：通过 `context.register_web_api()` 注册状态、配置、会话差异、任务、强度、通知、文档与目录操作接口。
- **视图 i18n**：`.astrbot-plugin/i18n/zh-CN.json` / `en-US.json` 提供页面标题与描述。
- **AstrBot 主题适配**：内嵌模式启用 AstrBot 蓝色色板、浅色/暗色表面、扁平卡片与顶部页签布局；MUI 主题同步切换。
- **轮询兜底**：受限 iframe 无法复用 Dashboard WebSocket，内嵌模式改用 3 秒轮询刷新状态与任务。

### 修复

- **AstrBot sandbox 兼容**：插件页面 iframe 没有 `allow-same-origin`，直接访问 `localStorage` 会抛 `SecurityError` 导致白屏；新增安全存储封装并替换所有直接读写。
- **AstrBot sandbox 弹窗**：`alert/confirm` 在没有 `allow-modals` 的 iframe 中会被忽略；配置保存失败提示、清空覆写与恢复默认确认改为页面内对话框。
- **bridge 注入时序**：AstrBot 将 bridge SDK 注入到 `</body>` 前，页面脚本可能先于 SDK 执行；适配层现在会检测页面上下文并等待 SDK 就绪。
- **视图资源越界**：视图不再引用 `../logo.png`，logo 移入视图目录，避免被 AstrBot 的路径令牌与 `base-uri` 拦截。
- **依赖冲突**：`requirements.txt` 不再要求 `fastapi>=0.142.2` 等高于 AstrBot Core 的版本，改为兼容区间，避免 `DependencyConflictError`。
- **Web 管理端静态目录**：独立管理端改为挂载 `views/proactive-chat/`，与插件页面共用同一份源码；保留 `admin/` 旧目录回退。

### 迁移

- 直接用 AstrBot 的「插件管理 → 重载」即可；页面视图会自动被发现。
- 如果仍在使用独立 WebUI，原地址与 token 登录方式不变。
- 旧部署目录中的 `admin/` 可保留，运行时会优先使用 `views/proactive-chat/`。

---

## v2.3.0 — 拟人化层

### 新增

- **作息节律**（`humanization_settings.rhythm_settings`）：
  - 从真实消息学习 24 小时活跃分布；
  - 活跃窗口外顺延，不再均匀随机落在半夜/上班时间；
  - 每日主动消息上限、最小间隔、周末放宽、昼夜概率系数。
- **关系状态机**（`state_settings`）：
  - `idle / open_thread / awaiting_reply / cooling / paused`；
  - 状态写入提示词，行为随关系阶段变化。
- **主动意图阶段**（`INTENT`）：
  - 延续话题 / 开新话题 / 关心 / 分享自己 / 提问 / 暂不发言；
  - 低精力、连续未回复、负反馈会提高“暂不发言”权重；
  - 连续沉默上限，避免一直不出现。
- **回复质量与负反馈**（`feedback_settings`）：
  - 实质 / 提问 / 敷衍 / 负反馈 / 媒体五类；
  - 敷衍回复不清零未回复计数；
  - 明确负反馈暂停主动消息，并记录暂停原因；
  - 按“小时桶 × 意图”统计反馈，用于意图加权。
- **发送前自然度守卫**（`guard_settings`，`GUARD` 阶段）：
  - 重复、过长、连续提问、AI 自指、索取情绪价值、客服腔；
  - 可选一次轻量 LLM 改写，仍不合格则放弃并重排期。
- **气泡发送计划**（`delivery_plan_settings`）：
  - 1–3 条气泡、按句切分、上下文延迟；
  - 语音改为按概率触发，不再“启用 TTS 就每条都发”；
  - 保留图片/语音等富媒体组件。
- **心情与精力**（`persona_settings`）：
  - 精力随时段起伏并回归基线；
  - 负反馈/连续未回复降低精力；
  - 低精力降低主动概率、缩短表达。
- **长期记忆接入**（`memory_integration_settings`）：
  - 复用 LivingMemory 的召回与写回，不重复实现记忆系统；
  - 兼容 session/global/user/legacy 作用域与 persona 过滤；
  - 未安装或未初始化时自动降级。
- **短期工作记忆**（`conversation_memory_settings`）：
  - 轻量用户画像：平均长度、提问比例、表情比例；
  - 开放话题：用户计划/事件、助手未回答的问题；
  - 话题账本注入提示词，并提高“延续话题/关心”意图权重；
  - 用户回应后闭环，超过 TTL 自动关闭。
- **P2 多阶段生成**（`generation_settings`）：
  - 可选 planner 生成话题/目标/语气/避免事项；
  - 可选 critic 做语义级自然度审稿，支持一次改写或放弃；
  - 任一阶段失败自动降级，不影响主流程。
- **离线回放**：`tools/replay_humanization.py` 用真实数据库时间线评估发送次数、概率未命中、暂停、回复质量与时段分布。
- **回复率修正**：敷衍/负反馈回复不再把上一条主动消息结算为“已回复”；`cooldown_until` 纳入 session 合并，重启/键规范化不丢失。
- **提示词上下文**：关系状态、意图 brief、心情/精力、活跃时段、每日预算、相对时间、长期记忆块、话题账本与 planner 计划。
- **WebUI**：互动强度/任务卡片展示关系状态、意图、心情与精力、活跃时段、回复质量、未闭环话题；运行状态展示长期记忆连接与负反馈暂停原因。
- 新增 101 项测试（总计 180 项），覆盖纯逻辑、服务、完整流水线集成与离线回放。

### 变更

- `HookRegistry` 新增 `PLAN`、`GUARD` 阶段；`INTENT` 阶段正式启用；DELIVER 阶段拆出 `DELIVERY_PLAN`。
- `SessionStore.merge_session_info` 新增拟人化状态、话题账本与 `cooldown_until` 的合并策略。
- 会话级覆写白名单新增 `humanization_settings`。

### 文档

- 新增 [docs/humanization.md](docs/humanization.md)；
- 更新 README、配置参考、管理端说明、强度模型与迁移文档。

---

## v2.2.0 — LLM 话题判定恢复

### 新增

- **LLM 话题判定恢复**（`pause_settings.resume_mode=llm_topic`）：
  - 暂停后用户新消息先经过一次轻量 LLM 判断；
  - 新话题/主动互动 → 恢复；旧话题/抱怨/无意义单字 → 保持暂停；
  - 支持专用 Provider、超时、上下文条数与失败兜底策略；
  - 图片/语音等非文本消息直接恢复；
  - 判定结果写入 `last_topic_judge`，WebUI 暂停列表可见。
- 新增 18 项暂停/话题判定测试（总计 79 项）。

---

## v2.1.0 — 连续未回复暂停

### 新增

- **连续未回复自动暂停**：
  - 达到 `schedule_settings.max_unanswered_times` 后写入 `proactive_paused`；
  - 清除下一次触发时间与调度字段，取消当前任务和自动触发计时器；
  - 暂停期间不再排期，不会反复“到点即停”；
  - 用户再次发来消息时自动解除暂停、未回复计数归零并按强度重新排期；
  - 暂停状态持久化，重启后仍保持。
- WebUI「运行状态」新增「未回复暂停」区块：
  - 展示暂停会话、连续未回复次数、阈值、暂停时间；
  - 支持手动「恢复并重新排期」。
- 暂停状态同步到互动强度卡片（「已暂停 · 等待用户新消息」）。
- 新增 API：`POST /api/jobs/{umo}/resume`。
- 暂停中的会话在「立即触发」「重新调度」接口返回 409，避免绕过暂停。
- 调度器、自动触发、启动恢复、持久化任务校验全部识别暂停状态。
- 新增 4 项暂停相关测试（总计 61 项）。

---

## v2.0.0 — 私聊主动消息 · 互动强度版

### 破坏性变更

- **移除群聊主动消息全链路**：
  - 删除群聊消息监听器 `on_group_message`；
  - 删除群沉默计时器 `group_timers` 与 `reset_group_silence_timer`；
  - 删除 `group_settings` 配置与 WebUI 群聊入口；
  - 群聊 UMO 在配置、调度、事件构造、流水线入口全部拒绝。
- 配置根键 `friend_settings` → `private_settings`。
  - 启动时自动迁移，旧键保留以便回滚。
- 删除 `TriggerSource.GROUP_IDLE`。

### 新增

- **互动强度引擎**（`core/domain/intensity.py` + `core/services/intensity_service.py`）：
  - 0–100 强度分：频率、节奏、连续活跃、主动回复率、表达欲；
  - 新鲜度指数衰减 + 低样本置信度；
  - 等级：冷淡 / 平稳 / 热络 / 亲密 / 炽热；
  - 强度 → 触发概率（15%–95%）；
  - 强度 → 间隔缩放（0.35–1.15，带最小间隔下限）；
  - 概率未命中的短重试。
- GATE 阶段概率掷骰，记录 `last_decision`；手动触发跳过概率。
- 主动消息回复结算：窗口内回复计入回复率与平均回复时长，超时标记未回复。
- 提示词新增占位符：
  - `{{intensity_score}}`
  - `{{intensity_tier}}`
  - `{{intensity_probability}}`
- 动态上下文注入互动强度与语气建议。
- WebUI 新增「互动强度」页：
  - 汇总卡片、等级筛选、搜索；
  - 每个会话的分数、概率、有效间隔、信号条形图、最近判定；
  - 30 秒自动刷新。
- 运行状态页重写为私聊版：强度概览、Top 5、自动触发计时器。
- 任务卡片显示强度、等级、概率与本次实际间隔。
- 历史导入工具 `tools/import_astrbot_history.py`：
  - 只读 `data_v4.db`，只导入私聊；
  - 生成强度报告；
  - 可写入 `session_data.json` 作为种子，自动备份。
- 强度模拟工具 `tools/simulate_intensity.py`。
- JSX 语法检查工具 `tools/check_jsx.js`。
- 57 项 pytest 测试。

### 优化

- 配置服务支持旧 `friend_settings` 迁移，非私聊 UMO 直接返回 `None`。
- 调度器只注册私聊任务，恢复时跳过并清理群聊历史数据。
- `core/services/__init__.py` 改为惰性导出，便于离线工具与测试单独导入。
- 文档重写：README、配置、强度、管理端、QQ 官方、迁移、导入。
- 元数据更新为 `v2.0.0`，描述与标签改为私聊/互动强度。

### 真实数据校准

- 784 个私聊会话、14,600 条用户消息；
- 峰值等级分布：冷淡 278 / 平稳 272 / 热络 147 / 亲密 82 / 炽热 5；
- 会话内消息间隔中位数 2 分钟，P75 5 分钟，P90 90 分钟。
