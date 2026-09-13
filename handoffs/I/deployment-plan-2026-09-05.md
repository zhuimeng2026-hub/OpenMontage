# VClaw × OpenMontage 重构 — 部署测试方案

**作者**：I（集成负责人 / WorkBuddy）
**日期**：2026-09-05
**状态**：待执行（待 I 与用户协同推进）

---

## 1. 当前落地状态（合并 + 推送后）

### 1.1 两仓 Git 状态

| 仓库 | 本地 HEAD | 远端 HEAD | 状态 |
|---|---|---|---|
| `C:/OpenMontage_voicebox` (OpenMontage) | `5388219` | `5388219` | ✅ 已同步 |
| `C:/vclaw` (VClaw) | `212bcca` | `212bcca` | ✅ 已同步 |

### 1.2 远端分支清单

| 仓库 | 分支 | 提交 SHA | 说明 |
|---|---|---|---|
| OpenMontage | `OpenMontage_Voicebox` | `5388219` | 集成分支（含 T00+T01+ .gitattributes） |
| OpenMontage | `codex/remix-om` | `06d032f` | A 线开发分支（历史存档） |
| VClaw | `master` | `212bcca` | 集成分支（含 Go+TS 契约 + .gitattributes） |
| VClaw | `remix-go` | `c5e502a` | B 线开发分支（Go 契约 + .gitattributes） |
| VClaw | `remix-gui` | `443ad1c` | C 线开发分支（TS 契约） |

### 1.3 关键契约固定值

- **fixture SHA256**：`4dd0c347e5cb71b62e14b1d1273c8d85617503024bcb58800b33789693d541f6`
  - 用于跨语言契约测试，任何 drift 即视为破坏
- **schema**：`schemas/remix/remix-package-v2.schema.json`（Python/Go/TS 三仓同字节）
- **错误码集合**：与评估报告 `01-contracts.md` C1–C9 一致

### 1.4 三语言契约测试 — 当前全绿

| 语言 | 命令 | 结果 |
|---|---|---|
| Python | `python -m pytest -q tests/remix/test_contract.py` | **17/17 PASS** |
| Go | `go test -count=1 ./internal/model/` | **PASS** |
| TS | `node --experimental-strip-types --test src/services/__tests__/remixContract.test.ts` | **17/17 PASS** |

---

## 2. 部署测试前必须先做的"安全检查"

### 2.1 B 盘共享源核对（核心风险点）

B 盘 (`\\192.168.20.173\voicebox`) 是远端在跑的 worker 正在消费的代码源。

**禁止**：
- ❌ 在 B 盘直接 `git pull`（会立即影响运行 worker）
- ❌ `git reset --hard` / `clean` B 盘
- ❌ 在 B 盘继续编辑生产 Python

**当前 B 盘 HEAD**：`a73f662 docs(vclaw-om): 2026-09-05 上传/重构评估 + 可执行修复计划 v1.1`（用户在 09:20:30 自己提交）

**B 盘状态调研清单**（部署前必查）：
```bash
cd B:/
git log --oneline -3                  # 确认 HEAD
git status --short | wc -l           # 是否有未提交改动
git fetch origin                     # 同步远端 refs
git log --oneline origin/OpenMontage_Voicebox..HEAD    # 是否有 B 独有 commit
git log --oneline HEAD..origin/OpenMontage_Voicebox    # 远端领先多少
```

### 2.2 远程 worker 状态

部署前确认：
- [ ] 当前 B 盘 OM 进程是否在跑（`ps` 或任务管理器查 `mcp_server.py` / `worker.py`）
- [ ] 是否有正在处理的视频上传任务
- [ ] 是否有正在渲染的视频
- [ ] 远端 worker 是否已配置自动 reload

---

## 3. 部署测试分层方案

### Tier 1 — 本地单元/集成测试（已通过 ✅）

```bash
# 三个语言独立跑
cd C:/OpenMontage_voicebox
python -m pytest -q tests/remix/test_contract.py        # 17/17

cd C:/vclaw
go test -count=1 ./internal/model/                       # 9/9

cd C:/vclaw-worktrees/gui/openclaw/clawx-studio
node --experimental-strip-types --test src/services/__tests__/remixContract.test.ts   # 17/17
```

### Tier 2 — 双仓端到端（待部署）

需要 mock 真实 MCP 上传，验证：

| 测试项 | 期望 | 阻塞修复项 |
|---|---|---|
| 上传视频 → VClaw → OM 接收 | 文件大小、SHA 与源一致 | F01 路径错误 |
| OM 上传分片 chunk 大小 | 与 VClaw 期望一致 | F02 固定 3 秒 |
| 同图重复检测 | 仅生成一次同图去重请求 | F03 同图去重 |
| 长视频（>201 镜头） | 不截断，全量处理 | F04 24 镜头截断 |
| VClaw 拆分 scene_plan | 时间戳精确到 ms，无重叠 | F05 时间戳精度 |
| TTS 选音 | 走 voicebox_tts，非 voice_clone | F06 voice 排除 |
| 渲染完成后回写 status | OM 记录 VClaw 真实进度 | F07 status 同步 |
| GUI direct 模式上传 | 不经中间层 | F08 direct 链路 |
| GUI 大文件分片（>50MB） | chunked upload 工作 | F09 传输分片 |

### Tier 3 — 部署到 B 盘运行源（最关键）

按计划 T22/T23 流程：

| 步骤 | 操作 | 风险控制 |
|---|---|---|
| 1 | 备份 B 盘当前状态到 `B:/_backup_pre_remix_20260905/` | 备份是单点失败的唯一保险 |
| 2 | 通知远端 worker 暂停新任务 | 防止新任务跑到一半 |
| 3 | 在 B 盘 `git fetch origin` | 不 pull，纯 fetch |
| 4 | `git log origin/OpenMontage_Voicebox --not HEAD` | 看要合并的 commits |
| 5 | 选发布窗口（凌晨或低峰） | 由用户决定 |
| 6 | 在 B 盘 `git merge --no-ff origin/OpenMontage_Voicebox` | 不重写 B 盘历史 |
| 7 | 重启 B 盘 OM 进程 | 验证重启可起 |
| 8 | 监控 30 分钟首条任务 | 确认无回归 |

---

## 4. 部署测试监控点

### 4.1 健康检查

- [ ] OM MCP 进程存活：`tasklist | grep mcp_server`
- [ ] VClaw Go BFF 监听 `:8080`：`curl http://127.0.0.1:8080/health`
- [ ] GUI Tauri 进程启动无 panic

### 4.2 业务冒烟

- [ ] 上传 1 个测试视频（>2 分钟）→ VClaw → OM 全链路
- [ ] 上传 1 个测试视频（>10 分钟）→ 验证不截断
- [ ] 上传 1 个含重复帧的视频 → 验证去重
- [ ] 触发 1 个渲染 → 检查 status 回写

### 4.3 性能基线

记录并对比基线：
- 上传 100MB 文件耗时
- VClaw 拆分 10 分钟视频耗时
- OM 渲染 24 镜头视频耗时
- 内存峰值

### 4.4 回滚触发条件（任意一项即触发）

- [ ] MCP 进程启动失败
- [ ] 上传测试 1 次失败率 > 50%
- [ ] 渲染结果损坏（视频无法播放）
- [ ] 日志出现新类型异常（未在已知 baseline）

---

## 5. 回滚路径

```bash
# 假设 B 盘已合并到 5388219，发现问题：

cd B:/
git log --oneline -3                                  # 确认当前位置
git reset --hard a73f662                              # 回到用户自己 commit 的版本
# 注意：reset --hard 会丢失 B 盘的本地改动，
# 所以务必先 git stash + git diff 确认无未提交改动

# 重启 OM 进程
# 或：rm -rf B:/_backup_pre_remix_20260905/
```

**前置条件**：步骤 1 的备份必须完整、可见、可恢复。

---

## 6. 部署测试协作分工

| 角色 | 负责 |
|---|---|
| 用户 | 选发布窗口；提供备份目录；确认 B 盘 worker 暂停；验证 GUI 启动 |
| I（WorkBuddy） | 监控日志；运行测试命令；分析失败原因；写 handoff |
| B 盘（如果自动） | 由备份脚本触发；不要自动重启 |

---

## 7. 部署测试时间预算

| 阶段 | 预计耗时 | 阻塞点 |
|---|---|---|
| Tier 1（已通过） | 5 分钟 | — |
| Tier 2 准备（mock + 脚本） | 30 分钟 | 缺 F02 修复 |
| Tier 3 部署（B 盘） | 1 小时 | 备份 + 用户授权 |

总计 ≥ 2 小时，含测试 + 监控 + 回滚预案。

---

## 8. 已知限制

1. **代码 worktree 状态**：C 线 worktree (`C:/vclaw-worktrees/gui`) 当前不在 `git worktree list` 中（已被 orphan 化），但分支 `remix-gui` 在主仓 C:/vclaw 仍可访问。需要清理 worktree metadata（删除 `C:/vclaw/.git/worktrees/gui/`）。
2. **B 线 worktree** (`C:/vclaw-worktrees/go`) 在 `remix-go` 分支 `c5e502a`，可用。
3. **A 线**：`codex/remix-om` 分支独立维护在 `C:/OpenMontage_voicebox`，后续任务可继续在此分支迭代。
4. **推送限制**：推送都用了 SSH (GitHub/Gitee)，如果未来切到 HTTPS token（来自 `.env`），需要更新远端 URL。

---

## 9. 立即可执行的下一步

| 优先级 | 任务 | 预计 |
|---|---|---|
| P0 | I 写 Tier 2 双仓端到端测试脚本（mock MCP 上传） | 30 分钟 |
| P0 | 清理 VClaw orphan worktree metadata | 5 分钟 |
| P1 | 用户决策 B 盘备份位置 + 发布窗口 | 等用户 |
| P1 | 用户提供 SSH/HTTPS 凭据（如未来切换） | 等用户 |
| P2 | I 继续推进 T02/T04/T06（在 codex/remix-om 上） | 等用户授权 G0 |

---

## 10. 一句话状态总结

代码侧已全部合并 + 推送，跨语言契约测试三语言全通过（Python 17/17、Go 9/9、TS 17/17），fixture SHA 锁定 `4dd0c347...`。部署前的关键瓶颈是 B 盘运行源的保护流程（备份 → 暂停 worker → 合并 → 监控 → 回滚），需要用户在选定的发布窗口授权执行。