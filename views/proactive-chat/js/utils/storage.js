/**
 * 文件职责：安全的 localStorage 封装。
 *
 * AstrBot 插件页面运行在 `sandbox="allow-scripts allow-forms allow-downloads"`
 * 的 iframe 中，没有 allow-same-origin，直接访问 localStorage 会抛
 * SecurityError。这里统一探测并降级为内存存储，保证页面不会因此白屏。
 */

(function () {
    const memory = new Map();
    let nativeStorage = null;
    try {
        const probeKey = '__proactive_storage_probe__';
        window.localStorage.setItem(probeKey, '1');
        window.localStorage.removeItem(probeKey);
        nativeStorage = window.localStorage;
    } catch (e) {
        nativeStorage = null;
    }

    function get(key, fallback) {
        try {
            const value = nativeStorage ? nativeStorage.getItem(key) : memory.get(key);
            return value === null || value === undefined ? fallback : value;
        } catch (e) {
            return fallback;
        }
    }

    function set(key, value) {
        const text = String(value);
        try {
            if (nativeStorage) {
                nativeStorage.setItem(key, text);
            } else {
                memory.set(key, text);
            }
        } catch (e) {
            memory.set(key, text);
        }
    }

    function remove(key) {
        try {
            if (nativeStorage) {
                nativeStorage.removeItem(key);
            }
        } catch (e) {
            // 忽略受限环境下的删除失败。
        }
        memory.delete(key);
    }

    window.StorageUtil = Object.freeze({
        available: Boolean(nativeStorage),
        get: get,
        set: set,
        remove: remove,
    });
})();
