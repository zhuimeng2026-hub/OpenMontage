# OpenMontage 代码审查标准与流程

> 适用对象：所有进入 `main` 的源码 Pull Request（Python / Go / TypeScript / JavaScript / YAML）。
> 维护者：代码审查专家（code-review-expert）。最新同步：`origin/main` @ `5d97c04`。

本文件与 AI 自审规范 `skills/meta/reviewer.md` **互补**：`reviewer.md` 审查**视频产物**（brief / script / scene_plan …），本文件审查**源码 PR**。两者共用同一套严重度词汇与"两轮上限"原则，确保人与 AI 审查认知一致。

---

## 0. 为什么需要这套机制

OpenMontage 的约定（架构、工具契约、流水线）在 `PROJECT_CONTEXT.md` / `AGENT_GUIDE.md` 里写得非常清楚，但**它们只存在于文档、未被 PR 门禁强制**——这是"代码质量参差不齐"的根因。

既有基础设施（地基已具备，需系统化）：

| 资产 | 状态 | 缺口 |
|------|------|------|
| `.github/ci.yml` | ✅ 仅 Python 的 lint + test | ❌ 不覆盖 Go / TS；无覆盖率门槛；无密钥扫描 |
| `.github/PULL_REQUEST_TEMPLATE.md` | ✅ 基础版 | ❌ 清单通用，缺架构 / 按语言检查项 |
| `.github/CODEOWNERS` | ⚠️ 所有文件 → 单人 `@calesthio` | ❌ 单人瓶颈 = 审查阻塞 + 质量单点风险 |
| `tests/`（unit / contract / qa） | ✅ 较完整 | ⚠️ 无覆盖率下限强制 |
| `Makefile`（lint / test / test-contracts / preflight） | ✅ | — |
| `skills/meta/reviewer.md` | ✅ 成熟（CHAI 准则、critical/suggestion/nitpick、2 轮上限） | ⚠️ 只审视频产物，不审源码 |

**目标：把已经写清楚的约定变成门禁，而不是发明新规则。**

---

## 1. 审查标准（审什么）

### 1.1 严重度模型

直接复用 `reviewer.md` 的体系，避免双套词汇：

| 标记 | 含义（= reviewer.md） | 处置 |
|------|----------------------|------|
| 🔴 **BLOCKER** | critical：不修不能合 | 必须修复才能 merge |
| 🟡 **SHOULD-FIX** | suggestion：应修 | 经审批可 defer，须在 PR 备注 |
| 💭 **NIT** | nitpick：可选 | 可选，不阻塞 |

**两轮上限**：同一 round 内反复往返不超过 2 轮；第 2 轮后带警告通过（`APPROVE_WITH_WARNINGS`），避免无限返工。

### 1.2 通用标准（所有语言）

- **正确性**：边界条件、空值、并发、异常路径是否被正确处理。
- **安全性**：输入校验、鉴权、注入（命令 / SQL / 路径）、密钥泄露。
- **可维护性**：命名、单一职责、是否依赖"魔法"隐式行为。
- **性能**：N+1、不必要的分配 / 拷贝、阻塞调用、未设超时的外部调用。
- **测试**：行为变更必须有测试；关键路径有覆盖。

### 1.3 OpenMontage 架构不变量（项目专属，最高优先级）

这些约定被破坏就是"质量参差不齐"的直接来源。把它们作为 PR 审查的硬清单：

| ID | 不变量 | 违反示例 |
|----|--------|----------|
| INV-1 | 工具必须继承 `BaseTool`，用 `.execute()` 返回 `ToolResult` | 写 `tool.run()` 或裸函数 |
| INV-2 | 工具发现走 `tools/tool_registry.py`，**禁止 ad hoc import** | 直接 `import tools.video.xxx` 绕过注册表 |
| INV-3 | **Python 只做工具 + 持久化**；禁止在 Python 写编排 / 创意 / 审查逻辑（那是 agent 职责） | 在 `.py` 里写流程决策 |
| INV-4 | 新增能力用 **selector + provider** 模式（一个 capability router + 每 provider 一个具体 tool） | 硬编码单一 provider |
| INV-5 | 类名 PascalCase **且无 `Tool` 后缀** | `VideoGenTool` |
| INV-6 | Canonical artifact 必须符合 `schemas/artifacts/*.schema.json` | 手写 JSON 不校验 |
| INV-7 | 失败必须 `ToolResult(success=False, error=...)`，**禁止静默吞错或未捕获异常** | `except: pass` |
| INV-8 | 外部进程调用禁止 `shell=True`；校验返回码 + 超时 | 拼接 shell 字符串 |
| INV-9 | 成本敏感操作走 `cost_tracker` 的 estimate → reserve → reconcile | 直接调付费 API |
| INV-10 | checkpoint 协议：gated stage 未经 `human_approved` 不得写 `completed` | 绕过门禁 |

> ✅ 抽查显示工具层合规度较高：112 个文件继承 `BaseTool`，且 `base_tool.py` 本身示范了正确的 subprocess 用法（`shutil.which()` + `subprocess.run()`，不带 `shell=True`）。约定"有人在认真维护"，只是缺门禁。

### 1.4 按语言检查重点

- **Python（525 文件，主体）**：type hints、Pydantic 配置模型、异常分层、结构化日志、async 安全、密钥走 env 不硬编码。
- **Go（37，frameflow + mcp-proxy）**：`gofmt` / `go vet`、error wrapping（`%w`）、context 传播、goroutine 泄漏、nil 检查、module 整洁。
- **TypeScript / React（remotion-composer/src，82）**：strict tsconfig、禁用 `any`、hooks 依赖规则、组件外不直接 DOM 操作、props 类型化、生产环境无 `console.log`。
- **JavaScript（28）**：ESLint、模块边界。
- **YAML（pipeline_defs）**：用 `pipeline_manifest.schema.json` 校验；`review_focus` / `success_criteria` 必须存在；无密钥。

### 1.5 安全（🔴 BLOCKER 永远）

硬编码密钥（必须为 0 命中）、`shell=True` 命令注入、路径遍历、依赖供应链风险、上传类工具的 SSRF、`tests/test_user_auth.py` 已存在 → **强制鉴权检查**、日志不得打印密钥。

---

## 2. 审查流程（怎么审）

### 2.1 分支与 PR 工作流

- 从 `main` 切分支，命名 `<type>/<short>`（如现有 `codex/frameflow-ecommerce-video`）。
- PR 必须关联 Issue（模板 "Related issue"）。
- **PR 规模上限**：单一逻辑关注点，建议 diff ≤ 400 行。过大的 PR 要求拆分。

### 2.2 角色与 CODEOWNERS

- **Author**：自检 + 跑 `make lint && make test` + 填 PR 模板。
- **Reviewer**：按本标准逐条核对，使用标准严重度。
- 🔴 **CODEOWNERS 当前全指向单人 `@calesthio`，是质量与吞吐的双重风险**。建议改为**目录级多 owner**（如 `tools/`、`lib/`、`remotion-composer/`、`frameflow/` 各指定 1–2 人），避免单点瓶颈。

### 2.3 审查清单（已折叠进 `.github/PULL_REQUEST_TEMPLATE.md`）

Required（任一未满足 = BLOCKER）：

- [ ] 工具继承 BaseTool 且用 `.execute()` 返回 ToolResult（INV-1）
- [ ] 无绕过 tool_registry 的 ad-hoc import（INV-2）
- [ ] Python 无编排 / 创意 / 审查逻辑（INV-3）
- [ ] 失败有显式 ToolResult 错误，无静默吞错（INV-7）
- [ ] 无 `shell=True`；外部调用有超时 + 返回码校验（INV-8）
- [ ] 无硬编码密钥 / API key（密钥扫描 0 命中）
- [ ] 行为变更有测试且 `make test` 通过

Recommended（SHOULD-FIX）：

- [ ] selector + provider 模式 / 无硬编码 provider（INV-4）
- [ ] 成本敏感操作走 cost_tracker（INV-9）
- [ ] 类型注解 / tsconfig strict / `go vet` 干净
- [ ] 关键 artifact 符合 schema（INV-6）

Nits：

- [ ] 命名 / 注释 / 日志可读性

### 2.4 CI 门禁（强化现有 `ci.yml`，分阶段）

现状只有 1 个 Python job。建议补齐：

- ➕ **Go job**：`gofmt -l` / `go vet` / `go test ./...`
- ➕ **前端 job**：ESLint + `tsc --noEmit` + build `remotion-composer`
- ➕ **契约测试**：复用已有 `make test-contracts`
- ➕ **密钥扫描**：gitleaks / trufflehog 作为 🔴 BLOCKER 门禁
- ➕ **覆盖率下限**：关键模块 Python ≥ 60%，未达阻断
- ➕ **YAML schema 校验**：`pipeline_defs/*.yaml` 跑 manifest schema

### 2.5 严重度与处置

- 🔴 BLOCKER → 必须修复才能 merge。
- 🟡 SHOULD-FIX → 应修；可经批准者确认 defer 并在 PR 备注。
- 💭 NIT → 可选。
- **两轮上限**（复用 AI 自审的 "max 2 rounds"）。
- 处置结论：`APPROVE` / `REQUEST_CHANGES` / `APPROVE_WITH_WARNINGS`。

### 2.6 建议 SLA

- 首次响应 ≤ 2 个工作日；小 PR（≤ 100 行）≤ 1 工作日；阻塞发布的 PR 优先。

---

## 3. 分阶段落地

| 阶段 | 内容 | 周期 |
|------|------|------|
| **Phase 0（立即）** | 本文件落地；CODEOWNERS 改多 owner；PR 模板扩充清单 | 本周 |
| **Phase 1（1 周）** | CI 增 Go / 前端 job + 密钥扫描 + 覆盖率下限 + YAML schema 校验 | 1 周 |
| **Phase 2（2–4 周）** | 分支保护（required reviews、stale review 自动失效）；按 INV 扫描存量代码分批修 | 2–4 周 |
| **Phase 3（持续）** | 人机共用严重度词汇；季度质量复盘（Bug 分布 / 根因 / 覆盖率趋势） | 持续 |

---

## 4. 与既有 AI 自审的关系

- `skills/meta/reviewer.md` = 审**视频产物**（brief / script / scene_plan …）；本文件 = 审**源码 PR**。二者互补不重叠。
- 共享严重度词汇（critical / suggestion / nitpick）+ 共享"两轮上限" → 团队认知一致，审查记录可直接对齐。

---

## 5. Reviewer 速查命令

```bash
make lint            # Python 静态检查
make test            # 单元测试
make test-contracts  # 契约测试（复用既有目标）
gitleaks detect      # 密钥扫描（建议加进 CI）

# 架构不变量快扫：
grep -rn "shell=True" tools lib        # 应为 0（INV-8）
grep -rln "BaseTool" tools             # 工具文件应全部命中（INV-1）
```
