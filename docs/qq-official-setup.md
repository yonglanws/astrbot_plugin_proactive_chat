# QQ 官方机器人私聊配置

本版本主要针对 AstrBot 的 `qq_official` / `qq_official_webhook` 适配器调优，但逻辑对所有私聊平台一致。

---

## 1. 确认平台已连接

在 AstrBot 的「平台」页面添加 QQ 官方机器人，填写：

- AppID
- AppSecret
- Token / 回调配置（按 AstrBot 版本要求）

启动后，AstrBot 日志里应该能看到平台 ID，例如：

```text
qq_official
```

> 注意：历史数据库里可能出现 `qq_offficial`（多一个 f）这类拼写。**以你当前 AstrBot 日志/`/sid` 输出的平台 ID 为准**，不要直接照抄旧数据库里的拼写。

---

## 2. 获取私聊 UMO

让目标用户私聊机器人，发送任意一条消息。在 AstrBot 日志中查找：

```text
unified_msg_origin = qq_official:FriendMessage:XXXXXXXXXXXXXXXX
```

或使用 `/sid` 指令（若已安装对应命令插件）。

UMO 三段：

| 段 | 示例 | 说明 |
| --- | --- | --- |
| platform_id | `qq_official` | AstrBot 平台实例 ID |
| message_type | `FriendMessage` | QQ 官方私聊 |
| target_id | `XXXXXXXXXXXXXXXX` | 用户 openid / ID |

把完整 UMO 填入：

```yaml
private_settings:
  session_list:
    - "qq_official:FriendMessage:XXXXXXXXXXXXXXXX"
```

---

## 3. 最小可运行配置

```yaml
private_settings:
  enable: true
  session_list:
    - "qq_official:FriendMessage:XXXXXXXXXXXXXXXX"
  schedule_settings:
    min_interval_minutes: 30
    max_interval_minutes: 600
    quiet_hours: "1-7"
    max_unanswered_times: 4
  intensity_settings:
    enable: true
```

重启/重载插件后，在 WebUI：

1. 「运行状态」确认插件运行中、调度器已启动；
2. 让用户再发一条私聊；
3. 「互动强度」出现该用户；
4. 「任务管理」出现下一次主动消息时间；
5. 点「立即触发」做一次端到端验证。

---

## 4. QQ 官方平台限制

不同版本的 QQ 官方机器人能力不同，请以官方文档与 AstrBot 适配器说明为准：

- 私聊需要用户先添加机器人/在允许范围内；
- 机器人主动私聊可能受平台推送频率、配额、沙箱环境限制；
- 部分环境只允许被动回复，主动消息可能发送失败；
- 发送失败时插件不会存档、不会递增未回复计数，并会重新排期。

如果「立即触发」一直失败：

1. 看 AstrBot 日志中的平台发送错误；
2. 确认该用户与机器人存在私聊会话；
3. 确认机器人有主动消息权限；
4. 先在 QQ 里手动让用户发一条消息，再触发。

---

## 5. 多机器人 / 多平台

`session_list` 里可以放多个平台、多个用户的 UMO：

```yaml
session_list:
  - "qq_official:FriendMessage:AAAA"
  - "qq_official_webhook:FriendMessage:BBBB"
  - "telegram:FriendMessage:123456789"
```

每个会话独立计算互动强度、独立调度、独立未回复计数。

---

## 6. 平台 ID 漂移

AstrBot 的 UMO 平台段来自平台实例 ID。以下情况可能导致旧 UMO 失效：

- 修改了机器人名称；
- 删除后重新添加平台；
- 从 `qq_official` 换成 `qq_official_webhook`。

插件在启动与消息处理时会调用 `normalize_session_id`，尽量把 UMO 归一到当前运行中的平台实例。如果仍然不匹配：

1. 用最新日志里的 UMO 替换 `session_list`；
2. 在 WebUI 的会话差异配置中清理旧会话覆写；
3. 需要时把旧 `session_data.json` 中的旧键迁移到新键（插件会尝试同目标兜底）。
