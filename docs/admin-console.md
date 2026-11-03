# Web 管理端

管理端现在有两种打开方式，共用 `views/proactive-chat/` 下的同一份前端源码：

1. **AstrBot 原生插件页面（推荐）**：AstrBot WebUI → 插件管理 → 本插件详情 → 「主动消息管理台」。页面运行在受限 iframe 中，通过 `window.AstrBotPluginView` bridge 调用插件 API，主题跟随 Dashboard，无需端口与二次登录。
2. **独立 Web 管理端**：插件启动独立 HTTP 服务，默认地址：

```text
http://127.0.0.1:4100
```

配置项见 `web_admin`：

```json
{
  "enabled": true,
  "host": "127.0.0.1",
  "port": 4100,
  "password": ""
}
```

> 前端通过 CDN 加载 React 18、MUI 5、Babel Standalone、marked、DOMPurify、mermaid。
> 国内时区/语言会自动使用 npmmirror，否则使用 unpkg。

---

## AstrBot 插件页面

AstrBot 会扫描插件目录下的 `views/<view_name>/index.html`；本插件使用：

```text
views/proactive-chat/
  index.html
  css/
  js/
  fonts/
  logo.png
```

页面标题与描述来自 `.astrbot-plugin/i18n/<locale>.json`：

```json
{
  "views": {
    "proactive-chat": {
      "title": "主动消息管理台",
      "description": "查看运行状态、互动强度、任务排期并调整私聊主动消息配置。"
    }
  }
}
```

后端 API 由 `core/console/page_api.py` 通过 `context.register_web_api()` 注册，路由统一带插件名前缀：

```text
/astrbot_plugin_proactive_chat/status
/astrbot_plugin_proactive_chat/config
/astrbot_plugin_proactive_chat/config-schema
/astrbot_plugin_proactive_chat/sessions
/astrbot_plugin_proactive_chat/session-config
/astrbot_plugin_proactive_chat/session-config/delete
/astrbot_plugin_proactive_chat/jobs
/astrbot_plugin_proactive_chat/jobs/reschedule
/astrbot_plugin_proactive_chat/jobs/trigger
/astrbot_plugin_proactive_chat/jobs/resume
/astrbot_plugin_proactive_chat/jobs/cancel
/astrbot_plugin_proactive_chat/intensity
/astrbot_plugin_proactive_chat/notifications
/astrbot_plugin_proactive_chat/notifications/read
/astrbot_plugin_proactive_chat/notifications/read-all
/astrbot_plugin_proactive_chat/notifications/refresh
/astrbot_plugin_proactive_chat/markdown-files
/astrbot_plugin_proactive_chat/markdown-file
/astrbot_plugin_proactive_chat/open-directory
/astrbot_plugin_proactive_chat/auth-info
```

页面侧只需调用 bridge：

```js
const data = await window.AstrBotPluginView.apiGet("status");
await window.AstrBotPluginView.apiPost("jobs/trigger", { umo });
```

几个与独立 WebUI 不同的约束：

- **鉴权**：由 AstrBot Dashboard 完成，页面内不再输入管理端密码；
- **实时刷新**：受限 iframe 拿不到 Dashboard WebSocket 凭据，页面改用 3 秒轮询；
- **存储**：iframe 没有 `allow-same-origin`，`localStorage` 会抛异常，前端统一使用 `StorageUtil` 内存降级；
- **弹窗**：iframe 没有 `allow-modals`，`alert/confirm` 改为页面内对话框；
- **资源路径**：只能使用视图目录内的相对路径，不能引用 `../logo.png` 等越界文件。

> 旧版 AstrBot 如果没有 `astrbot.api.web` 或 `context.register_web_api`，插件会跳过页面 API 注册，独立 WebUI 仍可正常使用。

---

## 页面

### 运行状态

- 插件运行状态、版本、运行时长、WebSocket 连接数；
- 调度器状态、待执行任务数、已跟踪会话数、自动触发计时器数；
- 互动强度概览：平均强度、高热度会话、平均触发概率、未回复暂停数、已跟踪用户；
- 长期记忆连接状态（LivingMemory 已连接/等待连接/未启用）；
- 强度 Top 5（点击可跳到互动强度页）；
- **未回复暂停**：列出因连续未回复或明确负反馈而暂停的私聊，显示暂停原因、回复质量、最近一次 LLM 话题判定结果，可手动「恢复并重新排期」；
- 自动触发计时器卡片：倒计时、进度、目标时间、强度等级、未回复次数；
- 快捷操作：刷新、任务管理、配置管理。

### 互动强度

按强度从高到低展示所有已知私聊：

- 强度分与等级（冷淡 / 平稳 / 热络 / 亲密 / 炽热）；
- 主动触发概率与概率条；
- 有效间隔 vs 基础间隔；
- 关系状态、最近意图、心情/精力、活跃时段、今日主动预算、未闭环话题数、最近回复质量；
- 24h / 7d 消息量、14d 活跃天数、中位间隔、平均长度、主动回复率、累计消息、30d 主动次数；
- 7 个构成信号的可视化条形图；
- 最后互动、下一次计划、最近一次概率判定（命中/未命中/随机值）；
- 是否启用、是否从数据库导入、未回复计数；
- 搜索 + 等级筛选 + 30 秒自动刷新。

### 任务管理

- 下一次执行时间、倒计时、进度条；
- 强度分、等级、概率、本次实际间隔；
- 关系状态、最近意图、心情/精力徽章；
- 「立即触发」：跳过概率判定，直接执行一次；暂停中的会话需先恢复；
- 「重新调度」：按当前强度与作息重新计算下一次时间；
- 「取消任务」：删除调度并清除持久化触发时间。

### 通知中心

- 远端通知列表、未读数、标记已读、全部已读、手动刷新；
- 通知接口细节见 [notification-api-spec.md](notification-api-spec.md)。

### 文档浏览

- 直接读取插件根目录与 `docs/` 下的 Markdown；
- 有路径穿越防护，只允许受信任目录；
- 支持 Markdown 渲染、代码高亮与 mermaid。

### 配置管理

- 全局配置：`private_settings`（含 `humanization_settings`）、`web_admin`、`notification_settings`、`telemetry_config`；
- 会话差异配置：选择单个私聊，编辑可覆写字段；
- 支持展开/收起、恢复默认、撤销、清空覆写、草稿本地缓存。

---

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/auth-info` | 是否需要登录 |
| POST | `/api/login` | 密码换 token |
| GET | `/api/status` | 运行状态 + 计时器 + 强度概览 + 长期记忆状态 |
| GET | `/api/intensity` | 强度快照列表 + 汇总 |
| GET | `/api/jobs` | 调度任务 |
| POST | `/api/jobs/{umo}/trigger` | 立即触发（暂停中的会话会返回 409） |
| POST | `/api/jobs/{umo}/reschedule` | 重新调度（暂停中的会话会返回 409） |
| POST | `/api/jobs/{umo}/resume` | 解除未回复暂停并重新排期 |
| DELETE | `/api/jobs/{umo}` | 取消任务 |
| GET | `/api/config` | 全局配置（隐藏密码） |
| POST | `/api/config` | 保存全局配置 |
| GET | `/api/config-schema` | `_conf_schema.json` |
| GET | `/api/session-config/sessions` | 已知私聊会话 |
| GET | `/api/session-config/{umo}` | 单会话 base/override/effective |
| POST | `/api/session-config/{umo}` | 保存覆写/生效配置 |
| DELETE | `/api/session-config/{umo}` | 清空覆写 |
| GET | `/api/notifications` | 通知列表 |
| POST | `/api/notifications/read` | 单条已读 |
| POST | `/api/notifications/read-all` | 全部已读 |
| POST | `/api/notifications/refresh` | 手动拉取 |
| GET | `/api/markdown-files` | 文档列表 |
| GET | `/api/markdown-files/{path}` | 文档内容 |
| POST | `/api/open-directory` | 在宿主机打开插件/数据目录 |
| WS | `/ws` | 实时状态推送 |

WebSocket 消息：

- 连接后发送一次 `full_update`；
- 客户端可发送 `{"type":"ping"}` 得到 `pong`；
- 发送 `{"type":"refresh"}` 得到新的 `full_update`；
- 服务端在配置保存、任务变更、通知变更时主动广播 `update`。

---

## 登录

- `web_admin.password` 为空：无需登录；
- 设置密码后：前端先调 `/api/auth-info`，再用 `/api/login` 换 token；
- token 通过 `Authorization: Bearer <token>` 或 WebSocket `?token=` 传递。

---

## 前端开发

WebUI 是纯静态文件，没有构建步骤：

```text
views/proactive-chat/
  index.html          # 入口 + CDN 加载 + 脚本队列
  css/                # 主题、布局、状态页、内容、强度页
                        # astrbot-page.css 为内嵌模式的 AstrBot Page 覆盖层
  js/
    app.jsx           # 根应用与视图路由
    views/            # Status / Intensity / Tasks / Config / Notifications / Docs
    components/       # 侧栏、顶栏、内嵌页面外壳、配置字段
    domain/           # 纯函数（时间、进度、会话）
    hooks/            # API、WebSocket、时钟
    utils/            # bridge、安全存储、页面内对话框、HTTP
```

修改 JSX 后刷新浏览器即可；入口会绕过缓存重新拉取脚本。
AstrBot 内嵌模式会通过 `utils/bridge.js` 自动切换 API 与主题；独立 WebUI 保持原有 HTTP/WebSocket 逻辑。

检查 JSX 语法：

```bash
cd tools
npm install
node check_jsx.js
```
