# Remotion Studio 汉化补丁

## 原理

Remotion Studio 的 UI 词条以**字符串字面量**形式散布在 `node_modules/@remotion/studio/` 的 ESM 和 CJS 编译产物中。本补丁通过精确的字符串替换（`"Compositions"` → `"合成列表"`）将这些词条中文化。

**补丁目标文件**（全部在 `node_modules/@remotion/studio/` 下）：

- `dist/esm/*.mjs` — ESM bundle（webpack/rspack 读取）
- `dist/esm/**/*.js` — ESM 子模块
- `dist/*.js` — CJS 入口（`require.resolve('@remotion/studio/previewEntry')` 指向这里）
- `dist/**/*.js` — CJS 子模块（含 UI 组件）

**词条字典**：`scripts/studio-zh-dict.json`（407 条，覆盖 80+ 高频 UI 词条）

## 使用方法

```bash
# 打补丁（npm install 后自动执行，也可手动运行）
npm run studio:zh

# 还原（如补丁导致白屏，立即还原）
node scripts/patch-studio-zh.js --restore
```

## postinstall 固化

`package.json` 中已添加 `postinstall` 钩子，每次 `npm install` 后自动打补丁（静默失败，不影响 install）。

```json
"scripts": {
  "studio:zh": "node scripts/patch-studio-zh.js",
  "postinstall": "node scripts/patch-studio-zh.js 2>/dev/null || true"
}
```

## 升级 Remotion 后的处理

`remotion upgrade` 会重新安装 `node_modules/@remotion/studio`，覆盖掉补丁。需要重新打：

```bash
node scripts/patch-studio-zh.js
```

补丁脚本会自动清理 webpack 缓存，下次启动 Studio 时会重新打包包含中文的新 bundle。

## 验证

启动 Studio 后访问 http://localhost:3001 ，检查以下位置是否有中文：

- 左侧栏 Tab：`合成列表`、`资产`
- 菜单：`文件`、`视图`、`合成`、`工具`、`帮助`
- 时间轴：播放控制、关键帧操作
- 设置面板

## 已知限制

- **修改 node_modules，本质脆弱**：升级 Remotion 必须重新打补丁
- **非线程安全**：多人协作时每人各自打补丁，不支持通过 git 共享
- **词条覆盖范围**：约 407 条高频词，Lambda/云渲染/AI Coding Agent/许可证/Figma 相关词条未翻译
- **字体名和代码标识符未翻译**：避免误伤

## 词条增补

编辑 `scripts/studio-zh-dict.json`，在 `translations` 数组中添加：

```json
{ "en": "English Text", "zh": "中文文本" }
```

然后重新运行 `npm run studio:zh`。
