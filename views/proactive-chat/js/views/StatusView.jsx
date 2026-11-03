(() => {
/**
 * 文件职责：状态页视图（私聊版）。
 *
 * 展示服务状态、调度概览、互动强度概览、强度 Top 会话与自动触发计时器。
 */

const { Box, Typography, Chip, Button } = MaterialUI;

const TIER_ORDER = ['dormant', 'steady', 'warm', 'close', 'blazing'];
const TIER_LABELS = {
    dormant: '冷淡',
    steady: '平稳',
    warm: '热络',
    close: '亲密',
    blazing: '炽热',
};

function StatusMetricRow({ label, value, emphasize = false, status = '' }) {
    return (
        <div className="status-metric-row">
            <Typography variant="body2" className="status-metric-label">{label}</Typography>
            <Typography
                variant="body2"
                className={`status-metric-value ${emphasize ? 'is-emphasize' : ''} ${status ? `is-${status}` : ''}`}
            >
                {value}
            </Typography>
        </div>
    );
}

function TierChip({ tier, label }) {
    return (
        <span className={`intensity-tier-chip is-${tier || 'dormant'}`}>
            {label || TIER_LABELS[tier] || '未知'}
        </span>
    );
}

function IntensityTopRow({ item, onClick }) {
    const score = Number(item.score || 0);
    return (
        <button type="button" className="intensity-top-row" onClick={onClick}>
            <div className="intensity-top-main">
                <Typography variant="body2" className="intensity-top-name">
                    {item.session_display_name || item.session_name || item.session_id}
                </Typography>
                <TierChip tier={item.tier} label={item.tier_label} />
            </div>
            <div className="intensity-top-bar-track">
                <div className="intensity-top-bar" style={{ '--intensity-width': `${Math.max(2, Math.min(100, score))}%` }}></div>
            </div>
            <div className="intensity-top-meta">
                <span className="intensity-top-score">{score.toFixed(1)}</span>
                <span className="intensity-top-prob">主动概率 {item.probability_percent}%</span>
            </div>
        </button>
    );
}

function AutoTriggerTimerCard({ timer, displayTimezone, nowMs, snapshotAtMs }) {
    const meta = React.useMemo(
        () => window.ProgressDomain.resolveStatusTimerCard(timer, nowMs, displayTimezone, snapshotAtMs),
        [timer, nowMs, displayTimezone, snapshotAtMs]
    );
    const unansweredCount = Number(meta.unanswered_count || 0);
    return (
        <div className={`card status-timer-card ${meta.accentClass} ${meta.status === 'urgent' ? 'is-urgent' : ''} ${meta.status === 'expired' ? 'is-expired' : ''}`}>
            <div className="status-timer-card-top">
                <div className="status-timer-title-block">
                    <Typography variant="subtitle2" className="status-timer-kicker">{meta.sectionTitle}</Typography>
                    <Typography variant="body1" className={`status-timer-session ${meta.hasAlias ? 'is-primary' : 'mono'}`}>
                        {meta.sessionDisplayName}
                    </Typography>
                    {meta.sessionSubText ? (
                        <Typography variant="caption" className="status-timer-session-sub mono">
                            {`UMO · ${meta.sessionSubText}`}
                        </Typography>
                    ) : null}
                </div>
                <div className="status-timer-chip-stack">
                    <div className={`status-timer-kind-badge ${meta.accentClass}`}>{meta.kindBadgeLabel}</div>
                    <Chip
                        label={window.formatUnansweredLabel(timer.unanswered_count, timer.max_unanswered_times)}
                        size="small"
                        color={unansweredCount > 0 ? 'warning' : 'default'}
                        variant={unansweredCount > 0 ? 'filled' : 'outlined'}
                    />
                </div>
            </div>

            <div className="status-timer-meta-row">
                <div className={`task-status-pill is-${meta.status}`}>{meta.statusLabel}</div>
                <div className="status-timer-category-pill">{meta.categoryLabel}</div>
                {timer.intensity_tier_label ? (
                    <TierChip tier={timer.intensity_tier} label={timer.intensity_tier_label} />
                ) : null}
            </div>

            <div className="status-timer-panel">
                <div className="status-timer-primary-row">
                    <div>
                        <Typography variant="caption" className="status-timer-label">倒计时</Typography>
                        <Typography variant="body2" className="status-timer-countdown">{meta.countdownText}</Typography>
                    </div>
                    <div className="status-timer-progress-value">{meta.progressPercent}%</div>
                </div>
                <div className="task-progress-track">
                    <div
                        className={`task-progress-bar is-${meta.status}`}
                        style={{ '--progress-width': `${meta.progressPercent}%` }}
                    ></div>
                </div>
            </div>

            <div className="status-timer-info-grid">
                <div className="status-timer-info-item">
                    <Typography variant="caption" className="status-timer-info-label">目标时间</Typography>
                    <Typography variant="body2" className="status-timer-info-value">{meta.targetText}</Typography>
                </div>
                <div className="status-timer-info-item">
                    <Typography variant="caption" className="status-timer-info-label">计时窗口</Typography>
                    <Typography variant="body2" className="status-timer-info-value">
                        {meta.window_seconds ? formatDuration(meta.window_seconds, { compact: true, maxUnits: 2 }) : '--'}
                    </Typography>
                </div>
            </div>
        </div>
    );
}

function StatusView({ onRefresh }) {
    const { state, dispatch } = useAppContext();
    const api = useApi();
    const status = state.status || {};
    const intensity = status.intensity_summary || {};
    const memoryStatus = status.memory_status || {};
    const nowMs = useNow();
    const [refreshState, setRefreshState] = React.useState({ phase: 'idle' });
    const displayTimezone = state.config?.displayTimezone || 'Asia/Shanghai';
    const snapshotAtMs = Number(state.statusReceivedAt || 0);

    const uptimeSeconds = Number(status.uptime_seconds || 0);
    const liveUptimeSeconds = uptimeSeconds + (
        snapshotAtMs > 0 && nowMs > snapshotAtMs
            ? Math.max(0, Math.floor((nowMs - snapshotAtMs) / 1000))
            : 0
    );
    const jobsCount = Number(status.jobs_count || 0);
    const sessionsCount = Number(status.sessions_count || 0);
    const autoTriggerTimers = Number(status.auto_trigger_timers || 0);
    const schedulerRunning = Boolean(status.scheduler_running);
    const pluginRunning = Boolean(status.running);

    const timerCards = React.useMemo(
        () => window.ProgressDomain.dedupeTimerCards(
            Array.isArray(status.auto_trigger_cards) ? status.auto_trigger_cards : []
        ),
        [status.auto_trigger_cards]
    );

    const tiers = intensity.tiers || {};
    const topSessions = Array.isArray(intensity.top_sessions) ? intensity.top_sessions : [];
    const averageScore = Number(intensity.average_score || 0);
    const averageProbability = Math.round(Number(intensity.average_probability || 0) * 100);
    const pausedSessions = Array.isArray(status.paused_sessions) ? status.paused_sessions : [];
    const [resumeState, setResumeState] = React.useState({ sessionId: '', phase: 'idle' });

    const handleResume = async (sessionId) => {
        if (resumeState.phase === 'loading') return;
        setResumeState({ sessionId, phase: 'loading' });
        try {
            await api.resumeJob(sessionId);
            setResumeState({ sessionId, phase: 'success' });
            await onRefresh();
        } catch (e) {
            setResumeState({ sessionId, phase: 'error' });
        }
    };

    const handleRefresh = async () => {
        if (refreshState.phase === 'loading') return;
        setRefreshState({ phase: 'loading' });
        try {
            await onRefresh();
            setRefreshState({ phase: 'success', at: Date.now() });
        } catch (e) {
            setRefreshState({ phase: 'error', at: Date.now() });
        }
    };

    const refreshLabel = refreshState.phase === 'loading'
        ? '正在刷新...'
        : refreshState.phase === 'success'
            ? '刷新完成'
            : refreshState.phase === 'error'
                ? '刷新失败，请重试'
                : '刷新控制台数据';

    return (
        <Box>
            <div className="dashboard-grid proactive-status-grid">
                <div className="span-4">
                    <div className="card status-panel-card status-panel-card-primary">
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 3 }}>
                            <div className="status-panel-icon status-panel-icon-blue">⚡</div>
                            <Typography variant="h6" sx={{ fontWeight: 700 }}>服务状态</Typography>
                        </Box>
                        <div className="status-metric-list">
                            <StatusMetricRow
                                label="插件状态"
                                value={pluginRunning ? '运行中' : '已停止'}
                                emphasize
                                status={pluginRunning ? 'success' : 'error'}
                            />
                            <div className="status-divider"></div>
                            <StatusMetricRow label="运行时长" value={formatDuration(liveUptimeSeconds, { compact: true, maxUnits: 4 })} emphasize />
                            <StatusMetricRow label="版本" value={status.version || '--'} />
                            {window.__PROACTIVE_EMBEDDED__ ? (
                                <StatusMetricRow label="页面模式" value="AstrBot 内嵌 · 自动轮询" />
                            ) : (
                                <StatusMetricRow label="WebSocket" value={`${Number(status.ws_connections || 0)} 个连接`} />
                            )}
                        </div>
                    </div>
                </div>

                <div className="span-4">
                    <div className="card status-panel-card">
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 3 }}>
                            <div className="status-panel-icon status-panel-icon-purple">⏱️</div>
                            <Typography variant="h6" sx={{ fontWeight: 700 }}>调度概览</Typography>
                        </Box>
                        <div className="status-metric-list">
                            <StatusMetricRow
                                label="调度器"
                                value={schedulerRunning ? '已启动' : '未启动'}
                                emphasize
                                status={schedulerRunning ? 'success' : 'error'}
                            />
                            <div className="status-divider"></div>
                            <StatusMetricRow label="待执行任务" value={`${jobsCount} 个`} emphasize />
                            <StatusMetricRow label="已跟踪会话" value={`${sessionsCount} 个`} />
                            <StatusMetricRow label="自动触发计时器" value={`${autoTriggerTimers} 个`} />
                        </div>
                    </div>
                </div>

                <div className="span-4">
                    <div className="card status-panel-card">
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 3 }}>
                            <div className="status-panel-icon status-panel-icon-pink">🔥</div>
                            <Typography variant="h6" sx={{ fontWeight: 700 }}>互动强度</Typography>
                        </Box>
                        <div className="status-metric-list">
                            <StatusMetricRow label="平均强度" value={`${averageScore.toFixed(1)} / 100`} emphasize />
                            <div className="status-divider"></div>
                            <StatusMetricRow label="高热度会话" value={`${Number(intensity.hot_count || 0)} 个`} />
                            <StatusMetricRow label="平均触发概率" value={`${averageProbability}%`} />
                            <StatusMetricRow label="未回复暂停" value={`${pausedSessions.length} 个`} />
                            <StatusMetricRow label="已跟踪用户" value={`${Number(intensity.tracked || 0)} / ${Number(intensity.total || 0)}`} />
                            <StatusMetricRow
                                label="长期记忆"
                                value={memoryStatus.enabled
                                    ? (memoryStatus.available ? '已连接 LivingMemory' : '等待连接')
                                    : '未启用'}
                            />
                        </div>
                        <button
                            type="button"
                            className="btn btn-action intensity-open-button"
                            onClick={() => dispatch({ type: 'SET_VIEW', payload: 'intensity' })}
                        >
                            查看互动强度详情
                        </button>
                    </div>
                </div>

                <div className="span-8">
                    <div className="card status-panel-card">
                        <Box className="status-section-head">
                            <div>
                                <Typography variant="h6" sx={{ fontWeight: 700 }}>强度 Top 5</Typography>
                                <Typography variant="caption" color="text.secondary">分数越高，主动概率越高、间隔越短。</Typography>
                            </div>
                            <div className="intensity-tier-legend">
                                {TIER_ORDER.map((tier) => (
                                    <TierChip key={tier} tier={tier} label={`${TIER_LABELS[tier]} ${Number(tiers[tier] || 0)}`} />
                                ))}
                            </div>
                        </Box>
                        {topSessions.length === 0 ? (
                            <div className="status-empty-block">
                                <div className="status-empty-icon">🫧</div>
                                <Typography variant="body2" color="text.secondary">
                                    还没有可展示的强度数据。等用户在已启用的私聊里发消息后，这里会自动出现排行。
                                </Typography>
                            </div>
                        ) : (
                            <div className="intensity-top-list">
                                {topSessions.map((item) => (
                                    <IntensityTopRow
                                        key={item.session_id}
                                        item={item}
                                        onClick={() => dispatch({ type: 'SET_VIEW', payload: 'intensity' })}
                                    />
                                ))}
                            </div>
                        )}
                    </div>
                </div>

                <div className="span-4">
                    <div className="card status-panel-card status-panel-card-primary">
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 2 }}>
                            <div className="status-panel-icon status-panel-icon-pink">🚀</div>
                            <Typography variant="h6" sx={{ fontWeight: 700 }}>快捷操作</Typography>
                        </Box>
                        <div className="status-actions-list">
                            <Button
                                variant="contained"
                                onClick={handleRefresh}
                                disabled={refreshState.phase === 'loading'}
                                startIcon={<span>{refreshState.phase === 'loading' ? '⏳' : refreshState.phase === 'success' ? '✅' : refreshState.phase === 'error' ? '⚠️' : '🔄'}</span>}
                                sx={{ borderRadius: 3, boxShadow: 'none', px: 2.25 }}
                            >
                                {refreshLabel}
                            </Button>
                            <button
                                type="button"
                                className="btn btn-action"
                                onClick={() => dispatch({ type: 'SET_VIEW', payload: 'tasks' })}
                            >
                                📋 打开任务管理
                            </button>
                            <button
                                type="button"
                                className="btn btn-action"
                                onClick={() => dispatch({ type: 'SET_VIEW', payload: 'config' })}
                            >
                                ⚙️ 调整强度参数
                            </button>
                        </div>
                        <Typography variant="caption" color="text.secondary" className="status-hint-text">
                            手动触发会跳过概率判定；自动任务仍会按互动强度决定是否发送。
                        </Typography>
                    </div>
                </div>

                {pausedSessions.length > 0 ? (
                    <div className="span-12">
                        <div className="card status-panel-card status-paused-card">
                            <Box className="status-section-head">
                                <div>
                                    <Typography variant="h6" sx={{ fontWeight: 700, mb: 0.5 }}>
                                        未回复暂停
                                    </Typography>
                                    <Typography variant="caption" color="text.secondary">
                                        连续多次主动消息未获回复后临时停用；用户再次发来消息会自动恢复，也可以在这里手动恢复。
                                    </Typography>
                                </div>
                                <span className="paused-section-count">{`${pausedSessions.length} 个会话`}</span>
                            </Box>
                            <div className="paused-session-list">
                                {pausedSessions.map((item) => {
                                    const busy = resumeState.sessionId === item.session_id && resumeState.phase === 'loading';
                                    const failed = resumeState.sessionId === item.session_id && resumeState.phase === 'error';
                                    const pausedAt = window.TimeDomain.toDate(item.paused_at);
                                    return (
                                        <div className="paused-session-row" key={item.session_id}>
                                            <div className="paused-session-main">
                                                <Typography variant="body2" className="paused-session-name">
                                                    {item.session_display_name || item.session_name || item.session_id}
                                                </Typography>
                                                <Typography variant="caption" className="paused-session-umo mono">
                                                    {item.session_id}
                                                </Typography>
                                            </div>
                                            <div className="paused-session-meta">
                                                <span className="paused-session-pill">
                                                    {item.pause_reason_label || '连续未回复'}
                                                    {item.pause_reason === 'negative_feedback'
                                                        ? ''
                                                        : ` ${item.paused_unanswered_count}/${item.paused_threshold || '—'}`}
                                                </span>
                                                {item.last_reply_quality ? (
                                                    <span className="paused-session-judge">
                                                        回复质量：{{
                                                            substantive: '实质回复',
                                                            question: '提问/接话',
                                                            perfunctory: '敷衍',
                                                            negative: '负反馈',
                                                            media: '媒体消息',
                                                        }[item.last_reply_quality] || item.last_reply_quality}
                                                    </span>
                                                ) : null}
                                                {Number(item.cooldown_remaining_seconds || 0) > 0 ? (
                                                    <span className="paused-session-time">
                                                        冷却中 · 约 {Math.ceil(Number(item.cooldown_remaining_seconds) / 60)} 分钟
                                                    </span>
                                                ) : null}
                                                {item.last_topic_judge && item.last_topic_judge.reason ? (
                                                    <span
                                                        className="paused-session-judge"
                                                        title={String(item.last_topic_judge.reason)}
                                                    >
                                                        {item.last_topic_judge.new_topic === false ? '判定：旧话题' : '判定：新话题'}
                                                    </span>
                                                ) : null}
                                                {pausedAt ? (
                                                    <span className="paused-session-time">
                                                        {formatFriendlyTime(pausedAt, displayTimezone)} 暂停
                                                    </span>
                                                ) : null}
                                                {failed ? <span className="paused-session-error">恢复失败</span> : null}
                                            </div>
                                            <button
                                                type="button"
                                                className="btn btn-action paused-session-action"
                                                onClick={() => handleResume(item.session_id)}
                                                disabled={busy}
                                            >
                                                {busy ? '恢复中...' : '恢复并重新排期'}
                                            </button>
                                        </div>
                                    );
                                })}
                            </div>
                        </div>
                    </div>
                ) : null}

                <div className="span-12">
                    <div className="status-timers-section">
                        <Box className="status-timers-header-row">
                            <div>
                                <Typography variant="h6" sx={{ fontWeight: 700, mb: 0.5 }}>
                                    {`自动触发计时器（当前共 ${timerCards.length} 个）`}
                                </Typography>
                                <Typography variant="caption" color="text.secondary">
                                    仅私聊会话。计时结束后会先按互动强度掷骰，再决定是否生成主动消息。
                                </Typography>
                            </div>
                        </Box>
                        {timerCards.length === 0 ? (
                            <div className="card tasks-empty-card">
                                <div className="tasks-empty-icon">🪄</div>
                                <Typography variant="h6" sx={{ fontWeight: 700, mb: 1 }}>当前没有自动触发计时器</Typography>
                                <Typography variant="body2" color="text.secondary">
                                    当私聊在设定时间内没有新消息，并且开启了自动触发时，这里会显示倒计时。
                                </Typography>
                            </div>
                        ) : (
                            <div className="status-timer-grid">
                                {timerCards.map((timer) => (
                                    <AutoTriggerTimerCard
                                        key={`${timer.timer_kind}-${timer.session_id}`}
                                        timer={timer}
                                        displayTimezone={displayTimezone}
                                        nowMs={nowMs}
                                        snapshotAtMs={snapshotAtMs}
                                    />
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </Box>
    );
}

window.StatusView = StatusView;
})();
