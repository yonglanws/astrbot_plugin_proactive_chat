(() => {
/**
 * 文件职责：互动强度视图。
 *
 * 展示每个私聊会话的强度分、等级、触发概率、有效间隔与构成信号，
 * 帮助管理员理解“为什么这个用户更容易/更不容易收到主动消息”。
 */

const { Box, Typography, Button, TextField } = MaterialUI;
const { useState, useEffect, useMemo, useCallback } = React;

const TIER_ORDER = ['dormant', 'steady', 'warm', 'close', 'blazing'];
const TIER_LABELS = {
    dormant: '冷淡',
    steady: '平稳',
    warm: '热络',
    close: '亲密',
    blazing: '炽热',
};
const COMPONENT_LABELS = {
    frequency: '聊天频率',
    tempo: '回复节奏',
    streak: '连续活跃',
    engagement: '主动回复意愿',
    expressiveness: '表达欲',
    recency: '新鲜度',
    volume: '样本置信度',
};
const REPLY_QUALITY_LABELS = {
    substantive: '实质回复',
    question: '提问/接话',
    perfunctory: '敷衍',
    negative: '负反馈',
    media: '媒体消息',
};

function TierChip({ tier, label }) {
    return (
        <span className={`intensity-tier-chip is-${tier || 'dormant'}`}>
            {label || TIER_LABELS[tier] || '未知'}
        </span>
    );
}

function SummaryCard({ icon, label, value, hint, accent }) {
    return (
        <div className={`card intensity-summary-card ${accent || ''}`}>
            <div className="intensity-summary-icon">{icon}</div>
            <div>
                <Typography variant="caption" className="intensity-summary-label">{label}</Typography>
                <Typography variant="h5" className="intensity-summary-value">{value}</Typography>
                {hint ? <Typography variant="caption" className="intensity-summary-hint">{hint}</Typography> : null}
            </div>
        </div>
    );
}

function ComponentBars({ components }) {
    const keys = Object.keys(COMPONENT_LABELS).filter((key) => components && components[key] !== undefined);
    if (keys.length === 0) return null;
    return (
        <div className="intensity-components">
            {keys.map((key) => {
                const value = Math.max(0, Math.min(100, Number(components[key] || 0)));
                return (
                    <div className="intensity-component-row" key={key}>
                        <span className="intensity-component-label">{COMPONENT_LABELS[key]}</span>
                        <div className="intensity-component-track">
                            <div className="intensity-component-bar" style={{ '--component-width': `${value}%` }}></div>
                        </div>
                        <span className="intensity-component-value">{value}</span>
                    </div>
                );
            })}
        </div>
    );
}

function MetricChip({ label, value }) {
    return (
        <div className="intensity-metric-chip">
            <span className="intensity-metric-chip-label">{label}</span>
            <span className="intensity-metric-chip-value">{value}</span>
        </div>
    );
}

function IntensityCard({ session, displayTimezone }) {
    const metrics = session.metrics || {};
    const interval = session.effective_interval_minutes || [0, 0];
    const lastAt = window.TimeDomain.toDate(metrics.last_user_at);
    const nextAt = window.TimeDomain.toDate(session.next_trigger_time);
    const replyRate = metrics.reply_rate === null || metrics.reply_rate === undefined
        ? '暂无样本'
        : `${Math.round(Number(metrics.reply_rate) * 100)}%`;
    const medianGap = metrics.median_gap_minutes === null || metrics.median_gap_minutes === undefined
        ? '--'
        : `${metrics.median_gap_minutes} 分钟`;
    const decision = session.last_decision || {};
    const decisionText = decision.will_send === undefined
        ? '尚未判定'
        : decision.will_send
            ? `命中（随机值 ${decision.roll}）`
            : `未命中（随机值 ${decision.roll}）`;

    return (
        <div className={`card intensity-card is-${session.tier || 'dormant'} ${session.paused ? 'is-paused' : ''}`}>
            <div className="intensity-card-head">
                <div className="intensity-card-title-block">
                    <Typography variant="subtitle2" className="intensity-card-kicker">
                        {session.session_name ? '私聊会话' : '私聊 UMO'}
                    </Typography>
                    <Typography variant="body1" className={`intensity-card-name ${session.session_name ? 'is-primary' : 'mono'}`}>
                        {session.session_display_name || session.session_name || session.session_id}
                    </Typography>
                    <Typography variant="caption" className="intensity-card-umo mono">
                        {session.session_id}
                    </Typography>
                </div>
                <div className="intensity-card-score-block">
                    <div className="intensity-score-number">{Number(session.score || 0).toFixed(1)}</div>
                    <TierChip tier={session.tier} label={session.tier_label} />
                </div>
            </div>

            <div className="intensity-card-badges">
                <span className={`intensity-status-pill ${session.enabled ? 'is-enabled' : 'is-disabled'}`}>
                    {session.enabled ? '已启用' : '未启用'}
                </span>
                {session.seed_source ? <span className="intensity-seed-pill">已从数据库导入</span> : null}
                <span className="intensity-unanswered-pill">未回复 {Number(session.unanswered_count || 0)} 次</span>
                {session.conversation_state?.state_label ? (
                    <span className="intensity-human-pill">🧠 {session.conversation_state.state_label}</span>
                ) : null}
                {session.conversation_state?.last_intent_label ? (
                    <span className="intensity-human-pill">🎯 {session.conversation_state.last_intent_label}</span>
                ) : null}
                {session.persona?.mood_label ? (
                    <span className="intensity-human-pill">🌤️ {session.persona.mood_label} · 精力 {session.persona.energy_percent}%</span>
                ) : null}
                {Number(session.conversation_memory?.open_loop_count || 0) > 0 ? (
                    <span className="intensity-human-pill">🧵 未闭环 {session.conversation_memory.open_loop_count} 个话题</span>
                ) : null}
                {session.paused ? <span className="intensity-paused-pill">已暂停 · 等待用户新消息</span> : null}
                {session.has_state ? null : <span className="intensity-muted-pill">暂无实时样本</span>}
            </div>

            <div className="intensity-probability-panel">
                <div className="intensity-probability-head">
                    <span>主动触发概率</span>
                    <strong>{session.probability_percent}%</strong>
                </div>
                <div className="intensity-probability-track">
                    <div
                        className="intensity-probability-bar"
                        style={{ '--probability-width': `${Math.max(1, Math.min(100, Number(session.probability_percent || 0)))}%` }}
                    ></div>
                </div>
                <div className="intensity-interval-row">
                    <MetricChip label="有效间隔" value={`${interval[0]} - ${interval[1]} 分钟`} />
                    <MetricChip label="基础间隔" value={`${(session.base_interval_minutes || [])[0]} - ${(session.base_interval_minutes || [])[1]} 分钟`} />
                </div>
            </div>

            <div className="intensity-metrics-grid">
                <MetricChip label="24h 消息" value={metrics.messages_24h ?? 0} />
                <MetricChip label="7d 消息" value={metrics.messages_7d ?? 0} />
                <MetricChip label="14d 活跃" value={`${metrics.active_days_14 ?? 0} 天`} />
                <MetricChip label="中位间隔" value={medianGap} />
                <MetricChip label="平均长度" value={`${metrics.avg_length_7d ?? 0} 字`} />
                <MetricChip label="主动回复率" value={replyRate} />
                <MetricChip label="累计消息" value={metrics.total_user_messages ?? 0} />
                <MetricChip label="30d 主动" value={`${metrics.proactive_count_30d ?? 0} 次`} />
                <MetricChip label="活跃时段" value={session.rhythm?.active_hours_label || '样本不足'} />
                <MetricChip
                    label="今日主动"
                    value={session.rhythm ? `${session.rhythm.daily_count ?? 0}/${session.rhythm.daily_limit ?? '—'}` : '--'}
                />
                <MetricChip
                    label="回复质量"
                    value={REPLY_QUALITY_LABELS[session.last_reply_quality] || '暂无'}
                />
            </div>

            <ComponentBars components={session.components_percent} />

            <div className="intensity-card-footer">
                <div>
                    <Typography variant="caption" className="intensity-footer-label">最后互动</Typography>
                    <Typography variant="body2" className="intensity-footer-value">
                        {lastAt ? window.formatFriendlyTime(lastAt, displayTimezone) : '--'}
                    </Typography>
                </div>
                <div>
                    <Typography variant="caption" className="intensity-footer-label">下一次计划</Typography>
                    <Typography variant="body2" className="intensity-footer-value">
                        {nextAt ? window.formatDateTime(nextAt, displayTimezone, { includeYear: true, includeSeconds: false }) : '未排期'}
                    </Typography>
                </div>
                <div>
                    <Typography variant="caption" className="intensity-footer-label">最近一次判定</Typography>
                    <Typography variant="body2" className={`intensity-footer-value ${decision.will_send === false ? 'is-miss' : ''}`}>
                        {decisionText}
                    </Typography>
                </div>
            </div>
        </div>
    );
}

function IntensityView() {
    const { state } = useAppContext();
    const api = useApi();
    const displayTimezone = state.config?.displayTimezone || 'Asia/Shanghai';
    const [data, setData] = useState({ sessions: [], summary: {} });
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [query, setQuery] = useState('');
    const [tierFilter, setTierFilter] = useState('all');
    const [refreshedAt, setRefreshedAt] = useState(0);

    const load = useCallback(async () => {
        setLoading(true);
        setError('');
        try {
            const result = await api.getIntensity();
            setData({
                sessions: Array.isArray(result.sessions) ? result.sessions : [],
                summary: result.summary || {},
            });
            setRefreshedAt(Date.now());
        } catch (e) {
            setError(e.message || '加载互动强度失败');
        } finally {
            setLoading(false);
        }
    }, [api]);

    useEffect(() => {
        load();
        // 强度依赖真实消息，低频轮询即可；WS 推送也会触发父级刷新。
        const timer = setInterval(load, 30000);
        return () => clearInterval(timer);
    }, [load]);

    const sessions = data.sessions || [];
    const summary = data.summary || {};

    const filtered = useMemo(() => {
        const keyword = query.trim().toLowerCase();
        return sessions.filter((item) => {
            if (tierFilter !== 'all' && item.tier !== tierFilter) return false;
            if (!keyword) return true;
            const haystack = [
                item.session_id,
                item.session_name,
                item.session_display_name,
            ].filter(Boolean).join(' ').toLowerCase();
            return haystack.includes(keyword);
        });
    }, [sessions, query, tierFilter]);

    const tierCounts = summary.tiers || {};

    return (
        <Box>
            <div className="intensity-view-head">
                <div>
                    <Typography variant="h6" sx={{ fontWeight: 700, mb: 0.5 }}>互动强度</Typography>
                    <Typography variant="body2" color="text.secondary">
                        强度越高，主动消息的触发概率越高、间隔越短。分数由最近消息密度、来回节奏、活跃天数、主动回复率与表达欲共同决定。
                    </Typography>
                </div>
                <Button
                    variant="contained"
                    onClick={load}
                    disabled={loading}
                    startIcon={<span>{loading ? '⏳' : '🔄'}</span>}
                    sx={{ borderRadius: 3, boxShadow: 'none', px: 2.25, flexShrink: 0 }}
                >
                    {loading ? '刷新中...' : '刷新强度'}
                </Button>
            </div>

            {error ? (
                <div className="card app-error-card">加载失败：{error}</div>
            ) : null}

            <div className="intensity-summary-grid">
                <SummaryCard icon="👥" label="跟踪会话" value={summary.total ?? sessions.length} hint={`有实时样本 ${summary.tracked ?? 0} 个`} />
                <SummaryCard icon="📊" label="平均强度" value={`${Number(summary.average_score || 0).toFixed(1)}`} hint="满分 100" accent="is-blue" />
                <SummaryCard icon="🔥" label="高热度会话" value={summary.hot_count ?? 0} hint="强度 ≥ 55" accent="is-pink" />
                <SummaryCard
                    icon="🎯"
                    label="平均触发概率"
                    value={`${Math.round(Number(summary.average_probability || 0) * 100)}%`}
                    hint={refreshedAt ? `更新于 ${window.formatDateTime(new Date(refreshedAt), displayTimezone, { includeSeconds: true })}` : '—'}
                    accent="is-purple"
                />
            </div>

            <div className="card intensity-toolbar">
                <div className="intensity-tier-filter">
                    <button
                        type="button"
                        className={`intensity-filter-button ${tierFilter === 'all' ? 'is-active' : ''}`}
                        onClick={() => setTierFilter('all')}
                    >
                        全部 <span>{sessions.length}</span>
                    </button>
                    {TIER_ORDER.map((tier) => (
                        <button
                            type="button"
                            key={tier}
                            className={`intensity-filter-button is-${tier} ${tierFilter === tier ? 'is-active' : ''}`}
                            onClick={() => setTierFilter(tier)}
                        >
                            {TIER_LABELS[tier]} <span>{Number(tierCounts[tier] || 0)}</span>
                        </button>
                    ))}
                </div>
                <TextField
                    size="small"
                    label="搜索会话 / UMO"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    sx={{ minWidth: 280 }}
                />
            </div>

            {loading && sessions.length === 0 ? (
                <div className="card tasks-empty-card">
                    <div className="tasks-empty-icon">🔥</div>
                    <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>正在计算互动强度...</Typography>
                    <Typography variant="body2" color="text.secondary">首次加载会读取所有已跟踪私聊的强度状态。</Typography>
                </div>
            ) : filtered.length === 0 ? (
                <div className="card tasks-empty-card">
                    <div className="tasks-empty-icon">🫧</div>
                    <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>
                        {sessions.length === 0 ? '还没有可展示的会话' : '没有符合筛选条件的会话'}
                    </Typography>
                    <Typography variant="body2" color="text.secondary">
                        {sessions.length === 0
                            ? '在 private_settings.session_list 中添加私聊 UMO，并让对方发一条消息后，这里就会出现强度数据。'
                            : '试试清空搜索词或切换到“全部”。'}
                    </Typography>
                </div>
            ) : (
                <div className="intensity-grid">
                    {filtered.map((session) => (
                        <IntensityCard key={session.session_id} session={session} displayTimezone={displayTimezone} />
                    ))}
                </div>
            )}
        </Box>
    );
}

window.IntensityView = IntensityView;
})();
