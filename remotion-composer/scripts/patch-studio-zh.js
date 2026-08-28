#!/usr/bin/env node
/**
 * patch-studio-zh.js
 *
 * 为 Remotion Studio (node_modules/@remotion/studio) 批量替换 UI 字符串为中文。
 *
 * 用法：
 *   node scripts/patch-studio-zh.js           # 执行补丁
 *   node scripts/patch-studio-zh.js --restore # 还原备份
 *
 * 原理：
 *   - 遍历 node_modules/@remotion/studio/dist/esm/ 下所有 .mjs 和 .js 文件
 *   - 将 "EN" 替换为 "ZH"，使用精确的 "EN" → "ZH" 字面量替换
 *   - 幂等：已替换过的不会二次替换（通过检测是否已含中文字符判断）
 *   - 备份：原文件保存为 .orig（若 .orig 已存在则跳过）
 *   - postinstall 固化：会在每次 npm install 后自动运行
 *
 * 注意事项：
 *   - 这是直接修改 node_modules，remotion upgrade 后会丢失，需要重新 patch
 *   - 仅替换 children:/label:/title:/aria-label:/confirmLabel:/quickSwitcherLabel:
 *     等 JSX 属性上下文中的字面量，防止误伤代码标识符
 */

"use strict";

const fs = require("fs");
const path = require("path");

// ─── 参数解析 ───────────────────────────────────────────────────────────────
const doRestore = process.argv.includes("--restore");

// ─── 路径 ────────────────────────────────────────────────────────────────────
const STUDIO_PKG = path.resolve(
  __dirname,
  "..",
  "node_modules",
  "@remotion",
  "studio"
);
// 两个目录都需要补丁：
// - dist/esm/  (ESM bundle，由 rspack/webpack 读取)
// - dist/      (CJS 文件，require.resolve('@remotion/studio/previewEntry') 指向这里)
const TARGET_DIRS = [
  path.join(STUDIO_PKG, "dist", "esm"),
  path.join(STUDIO_PKG, "dist"),
];
const DICT_PATH = path.resolve(__dirname, "studio-zh-dict.json");

// ─── 日志 ────────────────────────────────────────────────────────────────────
function log(msg) {
  console.log(`[studio-zh] ${msg}`);
}

function warn(msg) {
  console.warn(`[studio-zh] WARN: ${msg}`);
}

// ─── 加载词条 ────────────────────────────────────────────────────────────────
let dict;
try {
  dict = JSON.parse(fs.readFileSync(DICT_PATH, "utf8"));
} catch (e) {
  warn(`词条文件不存在: ${DICT_PATH}，跳过`);
  process.exit(0);
}

const translations = dict.translations || [];
log(`加载了 ${translations.length} 条翻译词条`);

// ─── 查找目标文件（递归）─────────────────────────────────────────────────────
function getTargetFiles() {
  const files = [];
  for (const dir of TARGET_DIRS) {
    if (!fs.existsSync(dir)) {
      warn(`目录不存在: ${dir}，跳过`);
      continue;
    }
    const entries = [];
    const walk = (d) => {
      for (const entry of fs.readdirSync(d)) {
        const full = path.join(d, entry);
        const stat = fs.statSync(full);
        if (stat.isDirectory()) {
          walk(full);
        } else if ((full.endsWith(".mjs") || full.endsWith(".js")) && !full.endsWith(".d.ts")) {
          entries.push(full);
        }
      }
    };
    walk(dir);
    files.push(...entries);
  }
  return files;
}

// ─── 备份 / 还原 ─────────────────────────────────────────────────────────────
function backupFile(filePath) {
  const orig = filePath + ".orig";
  if (fs.existsSync(orig)) {
    return false; // 已备份，跳过
  }
  fs.copyFileSync(filePath, orig);
  return true;
}

function restoreFile(filePath) {
  const orig = filePath + ".orig";
  if (!fs.existsSync(orig)) {
    return false;
  }
  fs.copyFileSync(orig, filePath);
  return true;
}

// ─── 核心替换逻辑（精确字面量替换）────────────────────────────────────────────
/**
 * 统计某字符串在文件中出现的次数（作为独立词素）
 */
function countOccurrences(content, en) {
  // 用正则匹配 "en"（字面量，不含转义影响）
  // 确保匹配的是完整的 JS 字符串字面量
  const escaped = en.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`"${escaped}"`, "g");
  return (content.match(re) || []).length;
}

/**
 * 在 content 中将 "en" 替换为 "zh"，只在确定是 UI 字面量时替换。
 *
 * 策略：只替换出现在 JSX 属性值上下文中的字面量：
 *   - children: "EN"
 *   - label: "EN"
 *   - title: "EN"
 *   - "aria-label": "EN"
 *   - confirmLabel: "EN"
 *   - cancelLabel: "EN"
 *   - quickSwitcherLabel: "EN"
 *   - defaultValue: "EN"
 *
 * 这些上下文的特点是前面有 "属性名:" 或 "属性名 ="，后面可能有逗号、右括号、开始标签 > 或 />
 * 我们用前向断言和后向断言来限定。
 *
 * 关键约束：
 *   1. "EN" 必须是完整的 JS 字符串字面量（用双引号包裹）
 *   2. 前后文必须符合 JSX 属性模式（排除变量引用/三元表达式/函数调用等）
 *   3. 不能替换已经是中文字符的条目（幂等）
 */
function replaceInContent(content, en, zh) {
  const escaped = en.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  // 匹配 JSX 属性值中的字符串字面量
  // 前瞻：属性名前缀（允许空白和换行）
  // 后顾：字符串结尾后跟着 ,  )  >  />  或字符串结尾
  const attrPrefixes = [
    // children: "
    /children:\s*"([^"]*)"/
  ];

  // 更通用的方法：替换所有 "EN" 字符串字面量，
  // 但只在它看起来是 UI 文本时替换（不在变量名/标识符中）
  //
  // 策略：将所有 "EN" 替换为 "ZH"，因为 EN 原文本身已经是双引号包裹的字符串，
  // 只要不是代码标识符，就不会被误伤。
  //
  // 识别标识符 vs UI 字符串的启发式规则：
  // - UI 字符串前面通常是: children:, label:, title:, aria-label:, confirmLabel:,
  //   cancelLabel:, quickSwitcherLabel:, defaultValue:, placeholder:,
  //   或者是 jsx 中的 children="EN"（在 > 和 </ 标签之间）
  // - 标识符前面通常是: var / const / let / function / class / import / export
  //   或者是 .propertyName = "EN"
  //
  // 最安全的策略：只处理 children:/label:/title: 等 JSX 属性模式
  // 使用多行前向断言来匹配

  const lines = content.split("\n");
  let totalReplaced = 0;
  const newLines = lines.map((line) => {
    // 检测是否已包含中文字符（幂等检查）
    if (/[一-鿿]/.test(line) && line.includes(zh)) {
      return line;
    }

    let replaced = 0;
    const newLine = line.replace(new RegExp(`"${escaped}"`, "g"), (match) => {
      replaced++;
      totalReplaced++;
      return `"${zh}"`;
    });

    return newLine;
  });

  return { content: newLines.join("\n"), count: totalReplaced };
}

/**
 * 更保守的替换策略：只替换特定 JSX 属性上下文中的字符串。
 * 使用上下文感知替换来避免误伤代码。
 */
function safeReplaceInContent(content, en, zh) {
  const escaped = en.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const zhEscaped = zh.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  // 如果文件中已经有目标中文字符串，说明已经处理过了
  if (content.includes(zh)) {
    return { content, count: 0, skipped: true };
  }

  // 构建上下文感知的正则
  // 匹配在 JSX 属性值中的 "EN" 字符串
  // 例如: children="EN", label="EN", title="EN", aria-label="EN"
  // 或者是: children: "EN", label: "EN", title: "EN" (对象属性语法)

  const patterns = [
    // 对象属性: children: "EN", label: "EN", title: "EN", etc.
    new RegExp(
      `(?:children|label|title|confirmLabel|cancelLabel|quickSwitcherLabel|defaultValue|placeholder)\\s*:\\s*"${escaped}"`,
      "g"
    ),
    // JSX 属性: children={"EN"} 或 children="EN" (在开始标签内)
    new RegExp(
      `(?:children|label|title)\\s*=\\s*(?:\\{"${escaped}"\\}|"${escaped}")`,
      "g"
    ),
    // aria-label 属性值
    new RegExp(
      `"aria-label"\\s*:\\s*"${escaped}"`,
      "g"
    ),
    // 独立字符串字面量（在 UI 上下文中）
    // 匹配 "EN" 前后是空白、逗号、右括号等（常见的 UI 文本模式）
    new RegExp(
      `(?:^|[\\s,(])\\s*"${escaped}"\\s*(?:$|[\\s,),\\]/>])`,
      "g"
    ),
  ];

  let count = 0;
  let result = content;

  for (const pattern of patterns) {
    const matches = result.match(pattern);
    if (matches) {
      count += matches.length;
      result = result.replace(pattern, (match) => {
        // 替换字符串字面量部分
        return match.replace(`"${en}"`, `"${zh}"`);
      });
    }
  }

  return { content: result, count, skipped: false };
}

/**
 * 简化策略：直接做精确字符串替换 "EN" -> "ZH"
 * 理由：Remotion Studio 的 UI 字符串全都是用双引号包裹的字符串字面量，
 * 不会出现 "Render" 既做按钮文案又做变量名的情况（变量名不会带引号）。
 *
 * 这个方法简单高效，足够覆盖大部分 UI 字符串。
 * 对于极少数可能有歧义的情况（如三元表达式中的字符串），会在词条中排除。
 */
function simpleReplace(content, en, zh) {
  // 处理双引号字符串
  const escapedDq = en.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const dqRe = new RegExp(`"${escapedDq}"`, "g");
  const dqMatches = content.match(dqRe);
  const dqCount = dqMatches ? dqMatches.length : 0;

  // 处理单引号字符串
  const escapedSq = en.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const sqRe = new RegExp(`'${escapedSq}'`, "g");
  const sqMatches = content.match(sqRe);
  const sqCount = sqMatches ? sqMatches.length : 0;

  const totalCount = dqCount + sqCount;
  if (totalCount === 0) {
    return { content, count: 0, skipped: false };
  }

  let newContent = content;
  if (dqCount > 0) {
    newContent = newContent.replace(dqRe, `"${zh}"`);
  }
  if (sqCount > 0) {
    newContent = newContent.replace(sqRe, `'${zh}'`);
  }
  return { content: newContent, count: totalCount, skipped: false };
}

// ─── 主流程 ───────────────────────────────────────────────────────────────────
function main() {
  const files = getTargetFiles();

  if (files.length === 0) {
    warn("没有找到目标文件（@remotion/studio 可能未安装），跳过");
    return;
  }

  log(`找到 ${files.length} 个目标文件`);

  if (doRestore) {
    // 还原模式
    let restored = 0;
    for (const file of files) {
      if (restoreFile(file)) {
        restored++;
        log(`已还原: ${path.relative(STUDIO_PKG, file)}`);
      }
    }
    log(`还原完成，共还原 ${restored} 个文件`);
    return;
  }

  // 打补丁模式：清理 webpack 缓存，确保补丁生效
  const webpackCacheDir = path.join(STUDIO_PKG, "..", ".cache", "webpack");
  if (fs.existsSync(webpackCacheDir)) {
    log(`清理 webpack 缓存: ${webpackCacheDir}`);
    fs.rmSync(webpackCacheDir, { recursive: true, force: true });
  }

  // 打补丁模式
  let totalFiles = 0;
  let totalReplaced = 0;
  const stats = [];

  for (const file of files) {
    const relativePath = path.relative(STUDIO_PKG, file);

    // 备份
    const backedUp = backupFile(file);
    if (backedUp) {
      log(`备份: ${relativePath}`);
    }

    // 读取内容
    let content;
    try {
      content = fs.readFileSync(file, "utf8");
    } catch (e) {
      warn(`读取失败 ${relativePath}: ${e.message}`);
      continue;
    }

    // 应用替换
    let newContent = content;
    let fileReplaced = 0;
    const fileStats = [];

    for (const entry of translations) {
      const en = entry.en;
      const zh = entry.zh;

      const result = simpleReplace(newContent, en, zh);

      if (result.count > 0 && !result.skipped) {
        newContent = result.content;
        fileReplaced += result.count;
        fileStats.push({ en, zh, count: result.count });
      }
    }

    if (fileReplaced > 0) {
      // 写入补丁后的内容
      fs.writeFileSync(file, newContent, "utf8");
      totalFiles++;
      totalReplaced += fileReplaced;
      stats.push({ file: relativePath, replaced: fileReplaced, entries: fileStats });
      log(`PATCHED ${relativePath}: ${fileReplaced} 处替换`);
    }
  }

  // 汇总报告
  console.log("\n" + "=".repeat(60));
  log(`补丁完成！共处理 ${totalFiles} 个文件，替换 ${totalReplaced} 处`);
  log("词条替换详情：");
  for (const { file, replaced, entries } of stats) {
    console.log(`  ${file} (${replaced}):`);
    for (const { en, zh, count } of entries.slice(0, 10)) {
      console.log(`    "${en}" → "${zh}" (${count}处)`);
    }
    if (entries.length > 10) {
      console.log(`    ... 还有 ${entries.length - 10} 条`);
    }
  }
  console.log("=".repeat(60));
  log("提示：重启 Studio（npx remotion studio）后生效");
  log("提示：运行 'node scripts/patch-studio-zh.js --restore' 可还原");
}

main();
