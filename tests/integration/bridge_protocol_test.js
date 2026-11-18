/**
 * 前端 iframe bridge 协议测试。
 *
 * 用 Node vm 模拟 AstrBot 插件页面环境，加载真实的
 * views/proactive-chat/js/utils/bridge.js，验证所有 /api/* 路由都按
 * AstrBotPluginView 的 apiGet/apiPost 协议正确映射。
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const calls = [];
const bridge = {
    ready: async () => ({ isDark: true, pageTitle: '主动消息' }),
    apiGet: async (endpoint, params) => {
        calls.push(['GET', endpoint, params || null]);
        return { endpoint, params: params || null };
    },
    apiPost: async (endpoint, payload) => {
        calls.push(['POST', endpoint, payload || null]);
        return { endpoint, payload: payload || null };
    },
    getContext: () => ({ isDark: true }),
    onContext: () => () => {},
    getLocale: () => 'zh-CN',
    t: (key, fallback) => fallback,
};

const window = {
    location: { pathname: '/plugin-page/astrbot_plugin_proactive_chat/proactive-chat' },
    AstrBotPluginView: bridge,
};
const document = {
    documentElement: { hasAttribute: () => true },
};

const sandbox = {
    window,
    document,
    console,
    URLSearchParams,
    Promise,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
};
vm.createContext(sandbox);

const bridgePath = path.resolve(
    __dirname,
    '../../views/proactive-chat/js/utils/bridge.js'
);
vm.runInContext(fs.readFileSync(bridgePath, 'utf8'), sandbox, {
    filename: bridgePath,
});

const api = window.ProactiveBridge;

async function main() {
    assert(api && api.embedded, 'bridge should detect embedded mode');

    await api.request('GET', '/api/status');
    await api.request('GET', '/api/config');
    await api.request('GET', '/api/config-schema');
    await api.request('GET', '/api/providers');
    await api.request('GET', '/api/session-config/sessions');
    await api.request('GET', '/api/jobs');
    await api.request('GET', '/api/intensity');
    await api.request('GET', '/api/notifications');
    await api.request('GET', '/api/markdown-files');

    const umo = 'qq_official:FriendMessage:USER_1';
    await api.request(
        'GET',
        `/api/session-config/${encodeURIComponent(umo)}`
    );
    await api.request('GET', '/api/markdown-files/docs%2Fconfiguration.md');
    await api.request('POST', '/api/config', {
        private_settings: { enable: true },
    });
    await api.request('POST', '/api/notifications/read', { id: 7 });
    await api.request('POST', '/api/notifications/read-all', {});
    await api.request('POST', '/api/notifications/refresh', {});
    await api.request('POST', '/api/open-directory', { target: 'data' });
    await api.request(
        'POST',
        `/api/session-config/${encodeURIComponent(umo)}`,
        { mode: 'effective', effective: { enable: true } }
    );
    await api.request(
        'POST',
        `/api/jobs/${encodeURIComponent(umo)}/trigger`,
        {}
    );
    await api.request(
        'POST',
        `/api/jobs/${encodeURIComponent(umo)}/resume`,
        {}
    );
    await api.request(
        'POST',
        `/api/jobs/${encodeURIComponent(umo)}/reschedule`,
        {}
    );
    await api.request('DELETE', `/api/session-config/${encodeURIComponent(umo)}`);
    await api.request('DELETE', `/api/jobs/${encodeURIComponent(umo)}`);

    const auth = await api.request('GET', '/api/auth-info');
    assert.deepStrictEqual(
        JSON.parse(JSON.stringify(auth)),
        { auth_required: false, embedded: true }
    );

    await assert.rejects(
        () => api.request('GET', '/api/unsupported'),
        /插件页面不支持该 GET 接口/
    );
    await assert.rejects(
        () => api.request('POST', '/api/unsupported', {}),
        /插件页面不支持该 POST 接口/
    );

    const expected = [
        ['GET', 'status', null],
        ['GET', 'config', null],
        ['GET', 'config-schema', null],
        ['GET', 'providers', null],
        ['GET', 'sessions', null],
        ['GET', 'jobs', null],
        ['GET', 'intensity', null],
        ['GET', 'notifications', null],
        ['GET', 'markdown-files', null],
        ['GET', 'session-config', { umo }],
        ['GET', 'markdown-file', { path: 'docs/configuration.md' }],
        ['POST', 'config', { private_settings: { enable: true } }],
        ['POST', 'notifications/read', { id: 7 }],
        ['POST', 'notifications/read-all', {}],
        ['POST', 'notifications/refresh', {}],
        ['POST', 'open-directory', { target: 'data' }],
        [
            'POST',
            'session-config',
            { mode: 'effective', effective: { enable: true }, umo },
        ],
        ['POST', 'jobs/trigger', { umo }],
        ['POST', 'jobs/resume', { umo }],
        ['POST', 'jobs/reschedule', { umo }],
        ['POST', 'session-config/delete', { umo }],
        ['POST', 'jobs/cancel', { umo }],
    ];
    assert.deepStrictEqual(JSON.parse(JSON.stringify(calls)), expected);
    console.log(JSON.stringify({ bridge_calls: calls.length, ok: true }));
}

main().catch((error) => {
    console.error(error);
    process.exit(1);
});
