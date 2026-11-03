#!/usr/bin/env node
/**
 * 前端 JSX 语法检查。
 *
 * 依赖：
 *   cd tools && npm install
 * 运行：
 *   node tools/check_jsx.js
 */

const fs = require('fs');
const path = require('path');

let parser;
try {
    parser = require('@babel/parser');
} catch (error) {
    console.error('缺少 @babel/parser，请先在 tools/ 目录执行 npm install。');
    process.exit(2);
}

const pluginRoot = path.resolve(__dirname, '..');
// 前端同时作为 AstrBot 插件页面（views/）和独立管理端，只维护一份源码。
const adminRoot = fs.existsSync(path.join(pluginRoot, 'views', 'proactive-chat'))
    ? path.join(pluginRoot, 'views', 'proactive-chat')
    : path.join(pluginRoot, 'admin');
let checked = 0;
let failed = 0;

function walk(directory) {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
        const fullPath = path.join(directory, entry.name);
        if (entry.isDirectory()) {
            walk(fullPath);
            continue;
        }
        if (!/\.(jsx|js)$/.test(entry.name)) continue;

        checked += 1;
        try {
            parser.parse(fs.readFileSync(fullPath, 'utf8'), {
                sourceType: 'script',
                plugins: [
                    'jsx',
                    'optionalChaining',
                    'nullishCoalescingOperator',
                    'classProperties',
                    'objectRestSpread',
                ],
            });
        } catch (error) {
            failed += 1;
            console.error(`FAIL ${path.relative(pluginRoot, fullPath)}: ${error.message}`);
        }
    }
}

walk(adminRoot);
console.log(`checked ${checked} files, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
