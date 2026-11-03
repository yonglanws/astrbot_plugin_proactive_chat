/**
 * 文件职责：会话领域纯函数，统一会话列表归一与展示信息推导。
 */

(function () {
    // 后端在不同接口中可能返回字符串数组或对象数组，这里统一收敛为对象形式。
    function normalizeSession(item) {
        if (typeof item === 'string') {
            const session = item.trim();
            if (!session) return null;
            return { session, has_override: false };
        }
        if (!item || typeof item !== 'object') return null;

        const session = String(item.session || item.id || '').trim();
        if (!session) return null;
        return Object.assign({}, item, { session });
    }

    function normalizeSessionList(list) {
        if (!Array.isArray(list)) return [];
        return list.map(normalizeSession).filter(Boolean);
    }

    // 本插件只处理私聊；保留该函数是为了兼容旧视图的调用点。
    function detectSessionType() {
        return 'friend';
    }

    // 统一计算会话展示名、UMO 副标题与别名标记，避免各视图重复判断。
    function resolveSessionDisplay(item) {
        const sessionId = String(item && (item.session || item.id) ? (item.session || item.id) : '');
        const rawName = item && (item.session_display_name || item.session_name);
        const displayName = String(rawName || sessionId || '--');
        const hasAlias = Boolean(displayName && sessionId && displayName !== sessionId);
        return {
            sessionId,
            displayName,
            hasAlias,
            subText: hasAlias ? sessionId : '',
        };
    }

    window.SessionDomain = Object.freeze({
        normalizeSession: normalizeSession,
        normalizeSessionList: normalizeSessionList,
        detectSessionType: detectSessionType,
        resolveSessionDisplay: resolveSessionDisplay,
    });
})();
