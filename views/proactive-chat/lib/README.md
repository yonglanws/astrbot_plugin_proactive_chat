# 前端本地依赖

这些文件是从官方 npm 包原样下载的 UMD/min 构建，随插件一起分发，避免离线或
内网环境因 CDN 不可达而白屏。升级版本时请同步更新 `index.html` 中的文件名与
`views/proactive-chat/lib/` 下的文件。

| 文件 | npm 包 | 版本 | 许可证 |
| --- | --- | --- | --- |
| `react.js` | `react` | 18.2.0 | MIT |
| `react-dom.js` | `react-dom` | 18.2.0 | MIT |
| `babel.min.js` | `@babel/standalone` | 7.25.6 | MIT（仅源码开发态需要，Release 会移除） |
| `mui.js` | `@mui/material` | 5.16.7 | MIT |
| `emotion-react.js` | `@emotion/react` | 11.13.3 | MIT |
| `emotion-styled.js` | `@emotion/styled` | 11.13.0 | MIT |
| `marked.min.js` | `marked` | 14.1.2 | MIT |
| `purify.min.js` | `dompurify` | 3.1.6 | Apache-2.0 OR MPL-2.0 |
| `mermaid.min.js` | `mermaid` | 11.4.1 | MIT |
