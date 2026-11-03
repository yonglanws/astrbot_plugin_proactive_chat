/**
 * 文件职责：AstrBot 插件页面 bridge 适配层。
 *
 * 同一份前端既可由独立 Web 管理端通过 HTTP/WebSocket 使用，也可作为
 * AstrBot `views/proactive-chat` 页面通过 `window.AstrBotPluginView` 使用。
 * 本文件只负责把现有的 `/api/*` 调用翻译成 bridge endpoint，业务代码无需
 * 感知运行环境。
 */

(function () {
    // AstrBot 会把 bridge SDK 注入到 </body> 前，而本文件由动态脚本队列加载，
    // 极端情况下可能先于 SDK 执行。因此不能只看当前是否存在 bridge，还要根据
    // 页面路径 / AstrBot 注入的 data-theme 判断是否处于插件页面，并等待 SDK。
    const path = String(window.location.pathname || '');
    const likelyEmbedded = (
        path.indexOf('/views/') >= 0 ||
        path.indexOf('/plugin-page/') >= 0 ||
        document.documentElement.hasAttribute('data-theme')
    );
    let bridge = window.AstrBotPluginView || null;
    const embedded = likelyEmbedded || !!bridge;
    let context = null;

    function waitForBridge(timeoutMs) {
        if (bridge && typeof bridge.ready === 'function') {
            return Promise.resolve(bridge);
        }
        return new Promise(function (resolve) {
            let elapsed = 0;
            const timer = setInterval(function () {
                if (window.AstrBotPluginView) {
                    bridge = window.AstrBotPluginView;
                    clearInterval(timer);
                    resolve(bridge);
                    return;
                }
                elapsed += 50;
                if (elapsed >= timeoutMs) {
                    clearInterval(timer);
                    resolve(null);
                }
            }, 50);
        });
    }

    const ready = embedded
        ? waitForBridge(3000)
              .then(function (api) {
                  if (!api || typeof api.ready !== 'function') {
                      return null;
                  }
                  return api.ready();
              })
              .then(function (value) {
                  context = value || null;
                  return context;
              })
              .catch(function (error) {
                  console.warn('[ProactiveBridge] 读取 AstrBot 页面上下文失败', error);
                  return null;
              })
        : Promise.resolve(null);

    function decodeSegment(value) {
        try {
            return decodeURIComponent(value);
        } catch (e) {
            return value;
        }
    }

    function splitUrl(url) {
        const raw = String(url || '');
        const hashIndex = raw.indexOf('#');
        const withoutHash = hashIndex >= 0 ? raw.slice(0, hashIndex) : raw;
        const queryIndex = withoutHash.indexOf('?');
        return {
            path: queryIndex >= 0 ? withoutHash.slice(0, queryIndex) : withoutHash,
            query: new URLSearchParams(queryIndex >= 0 ? withoutHash.slice(queryIndex + 1) : ''),
        };
    }

    function parseBody(body) {
        if (body === undefined || body === null || body === '') return {};
        if (typeof body === 'string') {
            try {
                return JSON.parse(body);
            } catch (e) {
                return {};
            }
        }
        return body;
    }

    function umoFromPath(path, prefix) {
        return decodeSegment(path.slice(prefix.length));
    }

    async function request(method, url, body) {
        if (!embedded) {
            throw new Error('AstrBot bridge 不可用');
        }
        const api = (bridge && typeof bridge.apiGet === 'function')
            ? bridge
            : await waitForBridge(3000);
        if (!api || typeof api.apiGet !== 'function') {
            throw new Error('AstrBot 页面 bridge 未就绪');
        }
        const normalizedMethod = String(method || 'GET').toUpperCase();
        const parsed = splitUrl(url);
        const path = parsed.path;
        const payload = parseBody(body);

        // 插件页面已经由 Dashboard 完成鉴权，不再走独立管理端的登录流程。
        if (path === '/api/auth-info') {
            return { auth_required: false, embedded: true };
        }
        if (path === '/api/login') {
            throw new Error('插件页面无需单独登录');
        }

        if (normalizedMethod === 'GET') {
            if (path === '/api/status') return api.apiGet('status');
            if (path === '/api/config') return api.apiGet('config');
            if (path === '/api/config-schema') return api.apiGet('config-schema');
            if (path === '/api/session-config/sessions') return api.apiGet('sessions');
            if (path === '/api/jobs') return api.apiGet('jobs');
            if (path === '/api/intensity') return api.apiGet('intensity');
            if (path === '/api/notifications') return api.apiGet('notifications');
            if (path === '/api/markdown-files') return api.apiGet('markdown-files');

            if (path.startsWith('/api/session-config/')) {
                return api.apiGet('session-config', {
                    umo: umoFromPath(path, '/api/session-config/'),
                });
            }
            if (path.startsWith('/api/markdown-files/')) {
                return api.apiGet('markdown-file', {
                    path: umoFromPath(path, '/api/markdown-files/'),
                });
            }
            throw new Error('插件页面不支持该 GET 接口: ' + path);
        }

        if (normalizedMethod === 'POST') {
            if (path === '/api/config') return api.apiPost('config', payload);
            if (path === '/api/notifications/read') {
                return api.apiPost('notifications/read', payload);
            }
            if (path === '/api/notifications/read-all') {
                return api.apiPost('notifications/read-all', payload);
            }
            if (path === '/api/notifications/refresh') {
                return api.apiPost('notifications/refresh', payload);
            }
            if (path === '/api/open-directory') {
                return api.apiPost('open-directory', payload);
            }
            if (path.startsWith('/api/session-config/')) {
                return api.apiPost('session-config', Object.assign({}, payload, {
                    umo: umoFromPath(path, '/api/session-config/'),
                }));
            }
            if (path.startsWith('/api/jobs/')) {
                const rest = path.slice('/api/jobs/'.length);
                const suffix = rest.slice(rest.lastIndexOf('/') + 1);
                const umo = decodeSegment(rest.slice(0, rest.lastIndexOf('/')));
                if (suffix === 'reschedule' || suffix === 'trigger' || suffix === 'resume') {
                    return api.apiPost('jobs/' + suffix, Object.assign({}, payload, { umo }));
                }
            }
            throw new Error('插件页面不支持该 POST 接口: ' + path);
        }

        if (normalizedMethod === 'DELETE') {
            if (path.startsWith('/api/session-config/')) {
                return api.apiPost('session-config/delete', {
                    umo: umoFromPath(path, '/api/session-config/'),
                });
            }
            if (path.startsWith('/api/jobs/')) {
                return api.apiPost('jobs/cancel', {
                    umo: umoFromPath(path, '/api/jobs/'),
                });
            }
            throw new Error('插件页面不支持该 DELETE 接口: ' + path);
        }

        throw new Error('插件页面不支持该请求方法: ' + normalizedMethod);
    }

    window.ProactiveBridge = {
        embedded: embedded,
        ready: ready,
        request: request,
        getContext: function () {
            return context || (bridge && typeof bridge.getContext === 'function'
                ? bridge.getContext()
                : null);
        },
        getLocale: function () {
            return bridge && typeof bridge.getLocale === 'function'
                ? bridge.getLocale()
                : 'zh-CN';
        },
        t: function (key, fallback) {
            return bridge && typeof bridge.t === 'function'
                ? bridge.t(key, fallback)
                : fallback;
        },
    };
})();
