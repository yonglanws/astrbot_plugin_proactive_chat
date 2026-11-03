# 从上游版本迁移

本版本是一次**私聊化重构**，不是简单的参数更新。迁移前请备份：

- AstrBot 插件配置（`data/config/astrbot_plugin_proactive_chat_config.json` 或对应文件）
- 插件数据目录（`data/plugin_data/astrbot_plugin_proactive_chat/`）

```text
session_data.json
session_overrides.json
```

---

## 0. v2.4.0 插件页面

v2.4.0 起，前端源码从 `admin/` 移到了 `views/proactive-chat/`，作为 AstrBot 原生插件页面加载；独立 WebUI 仍然由同一目录挂载。

- 升级后请在 AstrBot 里**重载插件**，让 Dashboard 重新扫描 `views/`；
- 如果旧部署里还保留 `admin/`，运行时会优先使用 `views/proactive-chat/`；
- 只用 AstrBot 插件页面时，可以把 `web_admin.enabled` 设为 `false`；
- 页面标题与描述在 `.astrbot-plugin/i18n/zh-CN.json` / `en-US.json` 的 `views.proactive-chat` 下修改。

---

## 0.1 v2.3.0 拟人化层

v2.3.0 新增 `private_settings.humanization_settings`，默认全部启用。旧配置无需改动即可运行；如果不想启用任何拟人化行为，可以逐项关闭：

```json
{
  "private_settings": {
    "humanization_settings": {
      "rhythm_settings": {"enable": false},
      "state_settings": {"enable": false},
      "feedback_settings": {"enable": false},
      "guard_settings": {"enable": false},
      "delivery_plan_settings": {"enable": false},
      "persona_settings": {"enable": false},
      "memory_integration_settings": {"enable": false},
      "conversation_memory_settings": {"enable": false},
      "generation_settings": {"planner_enabled": false, "critic_enabled": false}
    }
  }
}
```

新增的会话状态字段（`rhythm`、`conversation_state`、`persona_state`、`last_guard`、`last_proactive_texts`）与旧数据兼容；旧版插件不识别这些字段，但不会影响其读取其他字段。

长期记忆接入默认自动探测 `astrbot_plugin_livingmemory`；未安装时静默降级。详见 [humanization.md](humanization.md)。

---

## 1. 配置变化

### `friend_settings` → `private_settings`

旧：

```json
{
  "friend_settings": {
    "enable": true,
    "session_list": ["..."],
    "schedule_settings": { "...": "..." }
  },
  "group_settings": { "...": "..." }
}
```

新：

```json
{
  "private_settings": {
    "enable": true,
    "session_list": ["..."],
    "schedule_settings": { "...": "..." },
    "intensity_settings": { "...": "..." }
  }
}
```

启动时如果检测到：

- 有 `friend_settings`，没有 `private_settings`；
- 或 `private_settings` 不是对象；

会自动把 `friend_settings` 深拷贝为 `private_settings`，并记录日志：

```text
[主动消息] 检测到旧版 friend_settings，已自动迁移为 private_settings 喵。
```

旧键不会删除，方便回滚；确认稳定后可手动移除。

### `group_settings`

不再读取。可以删除，也可以保留（插件会忽略）。

### 新增 `intensity_settings`

不配置时使用默认值，见 [configuration.md](configuration.md#28-intensity_settings)。

---

## 2. 数据变化

`session_data.json` 结构保持兼容，新增每会话的 `intensity` 字段：

```json
{
  "qq_official:FriendMessage:AAAA": {
    "unanswered_count": 0,
    "last_message_time": 1760000000.0,
    "next_trigger_time": 1760003600.0,
    "intensity": {
      "version": 1,
      "events": [],
      "daily": {},
      "proactive": [],
      "total_user_messages": 0,
      "first_seen": 0,
      "last_user_ts": 0,
      "last_decision": {},
      "seed": {}
    }
  }
}
```

启动恢复任务时：

- 群聊键（`GroupMessage` / `GuildMessage`）会被跳过；
- 其 `next_trigger_time` 等调度字段会被清理；
- 私聊键正常恢复。

旧版没有 `intensity` 的会话会在下一次用户消息时自动创建。

---

## 3. 会话覆写

`session_overrides.json` 仍然兼容。变化：

- 新增允许字段 `intensity_settings`；
- 删除 `group_idle_trigger_minutes`（即使旧文件里有，也会被白名单过滤）。

清空覆写的方法：

- WebUI「配置管理 → 会话差异配置 → 清空会话覆写」；
- 或删除 `session_overrides.json` 中对应 UMO 的键。

---

## 4. 行为变化

| 行为 | 上游 | 本版本 |
| --- | --- | --- |
| 群聊消息 | 记录活跃、重置沉默计时 | **完全忽略** |
| 群聊主动消息 | 按沉默计时触发 | **不会发生** |
| 私聊下一次间隔 | 固定随机 | 按强度缩放 |
| 私聊触发 | 到点就生成 | 先按强度掷骰 |
| 手动触发 | 直接生成 | 仍直接生成（跳过概率） |
| 连续未回复 | 达到上限后停止 | 达到阈值后暂停并清除排期，用户新消息后自动恢复 |

如果上游的群聊会占用你的 token/额度，升级后这部分会停止。

---

## 5. 迁移步骤

1. **备份**配置与数据目录；
2. 替换插件代码为本版本；
3. 启动 AstrBot，查看日志：
   - 是否出现 `friend_settings` 自动迁移；
   - 是否出现群聊历史数据跳过；
4. 打开 WebUI：
   - 「运行状态」确认只显示私聊；
   - 「互动强度」确认会话出现；
   - 「配置管理」确认根键是 `private_settings`；
5. 让一个测试用户发私聊，确认：
   - 强度分变化；
   - 任务时间变化；
   - 「立即触发」能送达；
6. 可选：用 [import-history.md](import-history.md) 预热历史强度。

---

## 6. 回滚

如果必须回到上游版本：

1. 停用本插件；
2. 恢复旧插件代码；
3. 恢复备份的配置与 `session_data.json`；
4. 注意：本版本写入的 `intensity` 字段上游不识别，但不影响其读取其他字段；如需完全干净，删除各会话的 `intensity` 键。

---

## 7. 常见迁移问题

**Q：配置页看不到旧群聊配置了？**
A：本版本已删除 `group_settings`，这是预期行为。

**Q：旧私聊会话没有强度分？**
A：强度只统计启用后的消息。先让用户发消息，或导入历史。

**Q：启动日志说跳过了群聊数据？**
A：正常。插件会清理群聊的调度字段，但保留数据条目本身，避免误删。

**Q：会话 UMO 对不上？**
A：平台 ID 可能变了。用最新日志里的 UMO 替换 `session_list`，详见 [qq-official-setup.md](qq-official-setup.md#6-平台-id-漂移)。
