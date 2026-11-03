/**
 * 文件职责：页面内提示与确认对话框。
 *
 * AstrBot 插件页面 iframe 没有 allow-modals，window.alert/confirm 会被浏览器
 * 忽略（confirm 恒为 false）。这里用页面内浮层替代，独立管理端仍走原生弹窗。
 */

(function () {
    function isEmbedded() {
        return Boolean(
            window.__PROACTIVE_EMBEDDED__ ||
            document.documentElement.classList.contains('embedded-page')
        );
    }

    function closeOverlay(overlay) {
        if (overlay && overlay.parentNode) {
            overlay.parentNode.removeChild(overlay);
        }
    }

    function createOverlay(message, withCancel) {
        return new Promise(function (resolve) {
            const overlay = document.createElement('div');
            overlay.className = 'proactive-dialog-overlay';

            const dialog = document.createElement('div');
            dialog.className = 'proactive-dialog';
            dialog.setAttribute('role', 'dialog');
            dialog.setAttribute('aria-modal', 'true');

            const text = document.createElement('div');
            text.className = 'proactive-dialog-message';
            text.textContent = String(message || '');
            dialog.appendChild(text);

            const actions = document.createElement('div');
            actions.className = 'proactive-dialog-actions';

            function finish(value) {
                closeOverlay(overlay);
                resolve(value);
            }

            if (withCancel) {
                const cancel = document.createElement('button');
                cancel.type = 'button';
                cancel.className = 'btn proactive-dialog-button';
                cancel.textContent = '取消';
                cancel.addEventListener('click', function () {
                    finish(false);
                });
                actions.appendChild(cancel);
            }

            const confirm = document.createElement('button');
            confirm.type = 'button';
            confirm.className = 'btn proactive-dialog-button is-primary';
            confirm.textContent = withCancel ? '确定' : '知道了';
            confirm.addEventListener('click', function () {
                finish(true);
            });
            actions.appendChild(confirm);
            dialog.appendChild(actions);
            overlay.appendChild(dialog);

            overlay.addEventListener('click', function (event) {
                if (event.target === overlay) {
                    finish(withCancel ? false : true);
                }
            });
            overlay.addEventListener('keydown', function (event) {
                if (event.key === 'Escape') {
                    finish(withCancel ? false : true);
                }
            });

            document.body.appendChild(overlay);
            setTimeout(function () {
                confirm.focus();
            }, 0);
        });
    }

    window.ProactiveUI = {
        alert: function (message) {
            if (!isEmbedded()) {
                window.alert(message);
                return Promise.resolve(true);
            }
            return createOverlay(message, false);
        },
        confirm: function (message) {
            if (!isEmbedded()) {
                return Promise.resolve(window.confirm(message));
            }
            return createOverlay(message, true);
        },
    };
})();
