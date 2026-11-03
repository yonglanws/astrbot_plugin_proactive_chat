/**
 * 文件职责：管理端主题色板，集中维护 MUI 主题与 CSS 变量共用的颜色令牌。
 */

(function () {
    // 与 css/tokens.css 中的 --md-sys-color-* 保持一致，避免两处色值各自漂移。
    const PALETTES = Object.freeze({
        light: Object.freeze({
            mode: 'light',
            primary: '#6750A4',
            secondary: '#625B71',
            backgroundDefault: '#FEF7FF',
            backgroundPaper: '#FFFFFF',
            textPrimary: '#1C1B1F',
            textSecondary: '#49454F',
            divider: 'rgb(var(--md-primary-rgb) / 0.16)',
        }),
        dark: Object.freeze({
            mode: 'dark',
            primary: '#D0BCFF',
            secondary: '#CCC2DC',
            backgroundDefault: '#141218',
            backgroundPaper: '#1C1B1F',
            textPrimary: '#E6E1E5',
            textSecondary: '#CAC4D0',
            divider: 'rgb(var(--md-primary-rgb) / 0.16)',
        }),
        // AstrBot Dashboard 的 PurpleTheme / PurpleThemeDark 色板。
        // 插件页面通过 iframe 加载，不能继承父页面 Vuetify 主题，因此在本地复刻。
        astrbotLight: Object.freeze({
            mode: 'light',
            primary: '#3c96ca',
            secondary: '#2f86bd',
            backgroundDefault: '#ffffff',
            backgroundPaper: '#ffffff',
            textPrimary: '#1b1c1d',
            textSecondary: 'rgba(0, 0, 0, 0.66)',
            divider: 'rgba(0, 0, 0, 0.1)',
        }),
        astrbotDark: Object.freeze({
            mode: 'dark',
            primary: '#5ba4d4',
            secondary: '#4a95c4',
            backgroundDefault: '#1a1a1a',
            backgroundPaper: '#242424',
            textPrimary: '#e8eaed',
            textSecondary: 'rgba(255, 255, 255, 0.72)',
            divider: 'rgba(255, 255, 255, 0.14)',
        }),
    });

    const FONT_FAMILY = '"Roboto", "Noto Sans SC", "Helvetica", "Arial", sans-serif';

    function isEmbedded() {
        return Boolean(
            window.__PROACTIVE_EMBEDDED__ ||
            document.documentElement.classList.contains('embedded-page')
        );
    }

    function buildMuiTheme(mui, isDark) {
        let palette;
        if (isEmbedded()) {
            palette = isDark ? PALETTES.astrbotDark : PALETTES.astrbotLight;
        } else {
            palette = isDark ? PALETTES.dark : PALETTES.light;
        }
        return mui.createTheme({
            palette: {
                mode: palette.mode,
                primary: { main: palette.primary },
                secondary: { main: palette.secondary },
                background: {
                    default: palette.backgroundDefault,
                    paper: palette.backgroundPaper,
                },
                text: {
                    primary: palette.textPrimary,
                    secondary: palette.textSecondary,
                },
                divider: palette.divider,
            },
            typography: {
                fontFamily: isEmbedded()
                    ? '"Outfit", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif'
                    : FONT_FAMILY,
            },
        });
    }

    window.ThemeTokens = Object.freeze({
        PALETTES: PALETTES,
        FONT_FAMILY: FONT_FAMILY,
        buildMuiTheme: buildMuiTheme,
    });
})();
