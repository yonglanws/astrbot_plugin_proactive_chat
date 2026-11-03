/**
 * 文件职责：配置渲染公共常量与纯函数，供字段组件、加载 Hook 与主组件共用。
 */

(function () {
    // 递归扫描 Schema 中所有 object 节点，生成“全部展开 / 收起”需要的路径列表。
    function getAllExpandablePaths(schema, prefix) {
        const base = prefix || '';
        let paths = [];
        Object.entries(schema || {}).forEach(([key, value]) => {
            const currentPath = base ? `${base}.${key}` : key;
            if (value && value.type === 'object' && value.items) {
                paths.push(currentPath);
                paths = paths.concat(getAllExpandablePaths(value.items, currentPath));
            }
        });
        return paths;
    }

    // 为常见配置组与字段定义 emoji 图标，提升配置页的可扫读性。
    const CONFIG_ICONS = {
        private_settings: '💬',
        web_admin: '🌐',
        notification_settings: '🔔',
        telemetry_config: '📡',
        intensity_settings: '🔥',
        humanization_settings: '🧬',
        rhythm_settings: '🕰️',
        state_settings: '🧠',
        feedback_settings: '💬',
        guard_settings: '🛡️',
        delivery_plan_settings: '💨',
        persona_settings: '🌤️',
        memory_integration_settings: '🧩',
        conversation_memory_settings: '🧠',
        generation_settings: '🧩',
        pause_settings: '⏸️',
        resume_mode: '🔁',
        topic_judge_prompt: '🧭',
        topic_judge_timeout_seconds: '⏱️',
        topic_judge_context_messages: '🧩',
        fallback_resume_on_error: '🛟',
        topic_judge_provider_id: '🧠',
        auto_trigger_settings: '🤖',
        schedule_settings: '🕒',
        tts_settings: '🔊',
        segmented_reply_settings: '🔪',
        enable: '✅',
        session_list: '📋',
        proactive_prompt: '🧠',
        enable_auto_trigger: '✅',
        auto_trigger_after_minutes: '⏱️',
        min_interval_minutes: '⏱️',
        max_interval_minutes: '⏱️',
        quiet_hours: '🌙',
        max_unanswered_times: '🛑',
        enable_tts: '💬',
        always_send_text: '🔤',
        words_count_threshold: '📏',
        split_mode: 'Ⓜ️',
        regex: '🧩',
        split_words: '📝',
        interval_method: '⏱️',
        interval: '🎲',
        log_base: '📈',
        probability_min: '🎲',
        probability_max: '🎯',
        probability_gamma: '📈',
        interval_scale_hot: '🔥',
        interval_scale_cold: '🧊',
        interval_gamma: '📉',
        min_interval_floor_minutes: '⏳',
        response_window_hours: '💬',
        recency_half_life_hours: '🕯️',
        apply_to_auto_trigger: '🤖',
        prompt_context: '📝',
        skip_retry_ratio: '🔁',
        min_skip_retry_minutes: '⏱️',
        enabled: '✅',
        host: '📡',
        port: '🔌',
        password: '🔑',
        session_name: '🏷️',
    };

    // 某些 Schema 描述文本自身已带 emoji；这里剥离前缀，避免界面上图标重复出现。
    const LEADING_EMOJI_REGEX = /^\s*(?:\p{Extended_Pictographic}(?:\uFE0F|\u200D\p{Extended_Pictographic})*)\s*/u;

    function stripLeadingEmoji(text) {
        if (typeof text !== 'string') return text;
        return text.replace(LEADING_EMOJI_REGEX, '').trimStart();
    }

    // 会话差异配置页只暴露私聊全局配置中的可编辑字段子集。
    function getSessionSchemaEntries(schema) {
        const rootItems = (schema && schema.private_settings && schema.private_settings.items) || {};
        const orderedKeys = [
            'auto_trigger_settings',
            'proactive_prompt',
            'context_settings',
            'schedule_settings',
            'pause_settings',
            'intensity_settings',
            'humanization_settings',
            'tts_settings',
            'segmented_reply_settings',
        ];
        const hiddenKeys = new Set(['enable', 'session_list']);

        const entries = [];
        const sessionNameSchema = rootItems.session_name || {
            type: 'string',
            default: '',
            description: '会话备注名',
            hint: '用于日志和管理端展示。为空时将回退显示 UMO。',
        };
        entries.push(['session_name', sessionNameSchema]);

        orderedKeys
            .filter((key) => !hiddenKeys.has(key) && rootItems[key])
            .forEach((key) => {
                entries.push([key, rootItems[key]]);
            });

        return entries;
    }

    // 全局配置保存时提交的分组白名单必须与后端保持一致。
    const GLOBAL_CONFIG_KEYS = Object.freeze([
        'private_settings',
        'web_admin',
        'notification_settings',
        'telemetry_config',
    ]);

    window.ConfigShared = Object.freeze({
        CONFIG_ICONS: CONFIG_ICONS,
        GLOBAL_CONFIG_KEYS: GLOBAL_CONFIG_KEYS,
        getAllExpandablePaths: getAllExpandablePaths,
        stripLeadingEmoji: stripLeadingEmoji,
        getSessionSchemaEntries: getSessionSchemaEntries,
    });
})();
