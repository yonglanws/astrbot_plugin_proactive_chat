/** 
 * 文件职责：AstrBot 插件页面外壳。
 *
 * 作为 `views/proactive-chat` 页面运行时，Dashboard 已经提供左侧导航和顶栏，
 * 这里不再渲染独立管理端的侧边栏，而是使用 AstrBot Page 风格的标题、页签
 * 和内容卡片布局。
 */

(() => {
    const { Box, Typography } = MaterialUI;

    const VIEW_META = {
        status: {
            label: '运行状态',
            icon: '📊',
            subtitle: '查看插件、调度器、会话与未回复暂停的实时状态。',
        },
        intensity: {
            label: '互动强度',
            icon: '🔥',
            subtitle: '基于私聊频率、节奏、连续性与参与度计算主动消息概率。',
        },
        tasks: {
            label: '任务管理',
            icon: '📋',
            subtitle: '查看下一次触发时间，立即触发、恢复或取消会话调度。',
        },
        notifications: {
            label: '通知中心',
            icon: '🔔',
            subtitle: '查看插件运行通知与远端公告。',
        },
        docs: {
            label: '文档浏览',
            icon: '📚',
            subtitle: '在页面内阅读插件使用说明与配置文档。',
        },
        config: {
            label: '配置管理',
            icon: '⚙️',
            subtitle: '编辑全局私聊配置，保存后立即生效。',
        },
    };

    const VIEW_ORDER = ['status', 'intensity', 'tasks', 'notifications', 'docs', 'config'];

    function EmbeddedShell({ currentView, onChange, onRefresh, contentRef, children }) {
        const { state, dispatch } = useAppContext();
        const [refreshing, setRefreshing] = React.useState(false);
        const meta = VIEW_META[currentView] || VIEW_META.status;
        const unread = Math.max(0, Number(state.notificationsMeta?.unread_count ?? 0));

        const handleRefresh = async () => {
            if (refreshing || typeof onRefresh !== 'function') return;
            setRefreshing(true);
            try {
                await onRefresh();
            } finally {
                setRefreshing(false);
            }
        };

        const toggleTheme = () => {
            dispatch({ type: 'TOGGLE_THEME' });
        };

        return (
            <div className="embedded-page-shell">
                <header className="astrbot-page-header">
                    <div className="astrbot-page-heading">
                        <div className="astrbot-page-eyebrow">
                            <span className="astrbot-page-logo" aria-hidden="true">💬</span>
                            私聊主动消息
                        </div>
                        <Typography component="h1" className="astrbot-page-title">
                            {meta.label}
                        </Typography>
                        <Typography component="p" className="astrbot-page-subtitle">
                            {meta.subtitle}
                        </Typography>
                    </div>

                    <div className="astrbot-page-actions">
                        <span className="astrbot-page-live-pill">
                            <span className="astrbot-page-live-dot"></span>
                            自动刷新
                        </span>
                        <button
                            type="button"
                            className="btn astrbot-page-refresh"
                            onClick={handleRefresh}
                            disabled={refreshing}
                        >
                            {refreshing ? '刷新中...' : '刷新'}
                        </button>
                        <button
                            type="button"
                            className="astrbot-page-theme"
                            onClick={toggleTheme}
                            aria-label={state.theme === 'dark' ? '切换到亮色主题' : '切换到暗色主题'}
                            title={state.theme === 'dark' ? '切换到亮色主题' : '切换到暗色主题'}
                        >
                            {state.theme === 'dark' ? '🌞' : '🌙'}
                        </button>
                    </div>
                </header>

                <nav className="astrbot-page-tabs" aria-label="主动消息管理页面">
                    {VIEW_ORDER.map((key) => {
                        const item = VIEW_META[key];
                        const active = currentView === key;
                        return (
                            <button
                                key={key}
                                type="button"
                                className={`astrbot-page-tab${active ? ' is-active' : ''}`}
                                aria-current={active ? 'page' : undefined}
                                onClick={() => onChange(key)}
                            >
                                <span className="astrbot-page-tab-icon" aria-hidden="true">
                                    {item.icon}
                                </span>
                                {item.label}
                                {key === 'notifications' && unread > 0 ? (
                                    <span className="astrbot-page-tab-badge">
                                        {unread > 99 ? '99+' : unread}
                                    </span>
                                ) : null}
                            </button>
                        );
                    })}
                </nav>

                <main ref={contentRef} className="astrbot-page-content">
                    {state.error ? (
                        <div className="card app-error-card">错误：{state.error}</div>
                    ) : null}
                    {children}
                </main>
            </div>
        );
    }

    window.EmbeddedShell = EmbeddedShell;
})();
