# T00 Baseline — VClaw / OM 修复计划

| 字段 | 值 |
|---|---|
| 日期 | 2026-09-05 |
| 状态 | **G0 前置：T00 完成；T01 启动中** |
| 操作者 | I（WorkBuddy，gpt-5.6-luna） |
| 用户授权 | 用户明确授权"按文档要求处理 + 一条主线几个子代理并行" |

## 1. 真实路径映射

| 用途 | 路径 | 说明 |
|---|---|---|
| V_SRC | `C:\vclaw` | VClaw 主仓库，对应用户文档中的 `V` |
| GUI_SRC | `C:\vclaw\openclaw\clawx-studio` | GUI 在 V 仓库内 |
| OM_SRC | `B:\` | 共享运行源（`\\192.168.20.173\voicebox`） |
| **OM_DEV (A 线)** | `C:\OpenMontage_voicebox` | **新建立**——独立 OM 仓库，branch `codex/remix-om`，HEAD `caabcea` |
| **V_DEV (B 线)** | `C:\vclaw-worktrees\go` | **新建立**——git worktree，branch `codex/remix-go`，HEAD `8d04cfb` |
| **GUI_DEV (C 线)** | `C:\vclaw-worktrees\gui\openclaw\clawx-studio` | **新建立**——git worktree，branch `codex/remix-gui`，HEAD `8d04cfb` |

> 三个工作树之间互不干涉，分支命名遵循计划 §6：`codex/remix-{om,go,gui}`。

## 2. 版本与依赖

| 工具 | 版本 | 路径 |
|---|---|---|
| OS | Windows 10/11 | — |
| Python | 3.13.14（managed 3.13.12 实际指向 3.13.14） | `C:\Users\Admin\.workbuddy\binaries\python\versions\3.13.12\python.exe` |
| Node | 22.22.2 | `node --version` |
| Go | go1.26.5 windows/amd64 | `go version` |
| FFmpeg / FFprobe | 8.1.2-essentials_build-www.gyan.dev | PATH |
| Chromium / Chrome | 待验证 | T22 真实 GUI 验收时记录 |

## 3. 仓库 HEAD 与基线差异

| 仓库 | HEAD | 上游 | 备注 |
|---|---|---|---|
| B 盘 OM | `a73f662 docs(vclaw-om): 2026-09-05 上传/重构评估 + 可执行修复计划 v1.1` | `upstream/OpenMontage_Voicebox` | 用户在 09:20:30 独立提交了 docs |
| C 盘 OM（dev） | `caabcea docs: add vclaw-om assessment + repair plan + handoffs (2026-09-05)` | `origin/OpenMontage_Voicebox` ahead 3 commits | 含 merge `0905a05` + .gitignore `e9c49ca` + docs `caabcea` |
| C 盘 VClaw（主） | `8d04cfb fix(studio): unify production API routing` | — | 主分支 master，HEAD 同 v0.5.1 评估时 |
| B/C 线 worktree | `8d04cfb` | 新分支 codex/remix-go / codex/remix-gui | 与主仓库一致 |

**重要警告**：C 盘 OM 的 merge `0905a05` 把 `codex/frameflow-ecommerce-video` 的 620 文件改动合入，**HEAD 中删除了大量旧测试文件**。这是合并本身的设计意图——`codex/frameflow-ecommerce-video` 是一次大重构。T01 及后续任务需要**重新建立**被删的测试基础设施。

## 4. 基线测试结果

| 代号 | 命令 | 结果 | 备注 |
|---|---|---|---|
| **V-BASE** | `go test ./internal/handler ./internal/store ./internal/openclaw ./internal/openmontage` | ✅ **PASS** | handler 92.2s、store 61.5s、openmontage 1.0s；openclaw 无测试文件（已知） |
| **GUI-TYPE** | `vue-tsc --noEmit` | ✅ **PASS**（silent） | 无错误输出 |
| **OM-BASE** | `pytest tests/integration/` | ❌ **COLLECTION ERROR** | 3 个 integration 测试无法 import `from .conftest import ...`，属于合并后接口变更遗留 |

### baseline_failure 标记

- OM integration 测试 3 个文件 `test_voicebox_*.py`：相对 import 错误，T01 之后修复或重写
- OM `tests/test_asset_upload_chunk.py`、`tests/test_read_session_asset.py`、`tests/test_render_queue.py`：**HEAD 中已不存在**（被 merge 设计性删除）
- GUI `vitest`：**未安装**（package.json devDeps 无 vitest），T01 添加最小 vitest 配置

## 5. 已建立的工作树

```
$ git -C C:/vclaw worktree list
C:/vclaw               8d04cfb [master]
C:/vclaw-worktrees/go  8d04cfb [codex/remix-go]
C:/vclaw-worktrees/gui 8d04cfb [codex/remix-gui]
```

A 线 OM 在 `C:/OpenMontage_voicebox` 上独立分支 `codex/remix-om`，HEAD `caabcea`（包含合并、.gitignore 修复、文档入库）。

## 6. 声音克隆隔离状态

- `.quarantine/voice-clone-excluded/` 目录存在（3 文件：voicebox_voice_clone.py、voicebox_client.py、frameflow-voice-subtitle-minimax-mvp.md）
- `.gitignore` 已忽略 `/.quarantine/`
- HEAD 中**没有** `tools/audio/voicebox_voice_clone.py`
- 历史 commit `b786103` 和 `9ac1b50`（声音克隆相关）存在于合并的远端分支祖先中，但未进入 HEAD tree

## 7. 跨语言 fixtures 准备（待 T01）

`fixtures/remix-v2/contract-cases.json` 将由 T01 写入三个仓库：

- OM: `C:/OpenMontage_voicebox/fixtures/remix-v2/contract-cases.json`
- V: `C:/vclaw-worktrees/go/fixtures/remix-v2/contract-cases.json`
- GUI: `C:/vclaw-worktrees/gui/openclaw/clawx-studio/fixtures/remix-v2/contract-cases.json`

副本 SHA 必须相同，由三个测试在加载时校验。

## 8. 后续任务链

- **T01 即将启动**：v2 契约 schema、Go/TS/Python 类型、共享 fixtures、跨语言一致性 hash 测试
- T01 通过 → G0 放行
- G0 后分派 A/B/C 第一波（T02 / T06 / T04）

---

## 已知不一致 / 风险

1. B 盘 OM HEAD `a73f662` 与 C 盘 OM HEAD `caabcea` 不一致；C 盘多了 merge + .gitignore commits。push 待用户授权。
2. C 盘 OM 的合并导致大规模测试文件删除（合并设计意图）。T01 须重新建立测试基础设施。
3. GUI 没有 vitest，T01 需新增最小配置（仅 Remix/MCP 相关测试目录，不扫描第三方包）。
4. `B:/.workbuddy/memory/` 不可写（权限拒绝）；项目级 daily log 暂存 OM 仓库 `handoffs/I/`。

---

_Sign-off: I (WorkBuddy, 2026-09-05)_