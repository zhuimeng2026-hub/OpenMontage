# Decompose-path workspace-contract active guard — analysis & plan

> Companion doc to commit `1a77b11` (Plan A — observability) and the
> pre-implementation design of Plan B (active prevention). Records the
> forensic walkthrough that surfaced the root cause of the 2026-08-31
> 13:49 incident AND the design decisions behind the proposed write-time
> guard.

---

## Section 1 — Analysis process (the forensic walkthrough)

### 1.1 Starting point

The user flagged the video-decomposition path as "比较重要" and asked for **dedicated logging + dedicated monitoring** for it. The first round of work (Plan A) added:

- a new channel-separated log file `logs/decompose.log` (logger `decompose`)
- coarse-grained `event=decompose_phase` markers in the 4-phase orchestrator (`scripts/mcp_decompose_and_recompose.py`)
- `phase="decompose"` on per-tool `events.jsonl` events for `scene_detect`, `transcriber`, `video_analyzer`
- a new cron canary `tools/decompose_health_monitor.py` with three probes (scene_detect round-trip, decompose-log tail scan, **workspace-contract root walker**)

Plan A is committed on `release/mvp-v0.1-phase-0-5` as `1a77b11` and is purely **observational / post-hoc** — its Probe C scans `projects/` for stray files and alerts, but does not prevent the write.

### 1.2 The 13:49 anomaly discovered during planning

While preparing the Plan A implementation, a routine `ls -lt projects/` showed 24 files `frame_0000.jpg` … `frame_0023.jpg` sitting directly at the `projects/` **root**, dated 2026-08-31 13:49. All files ~100–230 KB jpg-encoded. Per CLAUDE.md invariant §5 ("Tool outputs go under `projects/<project-id>/`") this is a contract violation. The investigation focused on two questions:

1. Which code path wrote those files?
2. Was there a corresponding `script.json` (the canonical Phase-1 production artifact), or did this activity bypass the decompose pipeline entirely?

### 1.3 Forensic trail

| Source | Observation |
|---|---|
| `ls -lt /opt/OpenMontage_Voicebox/projects/frame_*.jpg` | 24 jpgs, mtime 2026-08-31 13:49, sizes 116–232 KB (consistent with FFmpeg `%04d` jpg extraction from a ~24s source at 1 fps or 24 keyframes from scene-guided mode) |
| `logs/mcp_health.log` 13:45–14:00 | Heartbeat at 13:48:35 shows `tool_pending=0 detail=upload_asset:0,video_compose:0,weiyun_upload:0,weiyun_share_link:0` — only baseline MCP tools listed. From 13:48:55 a burst of `upload_asset_chunk` (≈40 submit/done pairs in 15 s). At 13:50:05 heartbeat shows `executor_threads=5` (was 1 earlier — threads spawned). After 13:50:35 the heartbeat stabilizes again at 0 pending. **No `tool=video_analyzer` and no `tool=frame_sampler` lines appear anywhere in this window.** |
| `logs/mcp_server.log` 13:49:30–14:09 | Big burst of `read_session_asset` calls at 13:49:35 from `192.168.20.168` (session_hash `58fb18182d1928b1`). At 14:03:48 three `video_compose` calls from `127.0.0.1` (session_hash `02def15bd97d80e4`) — **all three fail** with `error: asset_manifest required for render`. |
| `projects/_scratch/events.jsonl` | Last entry is `2026-08-29T13:53:08` (3 days prior). The 13:49 activity produced **no structured events**. |
| `projects/<id>/artifacts/script.json` (search across all projects) | Most recent canonical script is `projects/the-refactor-serenade/artifacts/script.json` from `2026-08-28 18:36:32`. Nothing newer. |
| Session analytics at the boundary | Two distinct actors: upload window from `192.168.20.168` (session `58fb1818…`), later video_compose attempts from `127.0.0.1` (session `02def15…`) — same host, different sessions, no shared project_id binding. |

### 1.4 Root-cause inference

Putting the trail together:

1. The frame extraction was driven by **something that did not go through `BaseTool.execute()`**, because:
   - `_instrument_execute` (`tools/base_tool.py:220-296`) is the only path that emits to `events.jsonl`. It was silent during the window.
   - `mcp_server.py:987` (the `scene_detect` MCP wrapper) and the analogous `video_analyzer` / `transcriber` wrappers do not show up in `mcp_health.log` for this window.
   - `tools/asset_upload_chunk.py` only handles `project_id` + chunked binary upload; it does **not** run keyframe extraction.
2. The frame extraction almost certainly came from a direct Python invocation of `tools.analysis.frame_sampler.FrameSampler()` or `tools.analysis.video_analyzer._extract_*()` (line 110 uses `Path(inputs.get("output_dir", input_path.parent / "frames"))` followed by `mkdir(parents=True, exist_ok=True)`). If the caller passed `output_dir="projects"` or `"."`, the resulting path resolved to `PROJECTS_DIR/` root.
3. There is **no `script.json`, no `scene_plan.json`, no `asset_manifest.json`** corresponding to this run — confirming the activity did not enter the canonical `pipeline_defs/*.yaml` flow. This was "half a job": extraction done, decomposition absent.
4. The 14:03 `video_compose` failures are downstream: the caller had no project_id binding → `asset_manifest required for render`. This isn't actually part of the 13:49 incident, but it shows the same root cause — a session without proper project_id binding tried to render.

### 1.5 Why Plan A alone is insufficient

Plan A's `probe_workspace_contract` is **post-hoc detection** — it observes that files exist where they shouldn't, and alerts. That's valuable, but:

- The bad write has already happened by the time the probe ticks.
- If the side-effect is non-trivial (writes to disk, propagates downstream, etc.), rollback is manual.
- A malicious or buggy script run at 13:49 today would still write 24 jpgs (Plan A would only catch it on the next 5-minute Probe C cycle).

The user's most recent question framed this directly: "now this thing — how is it resolved?". Honest answer: Plan A closes the visible symptom; the root cause — that any `BaseTool` subclass can be called with an arbitrary `output_dir` and will `mkdir(parents=True, exist_ok=True)` straight onto disk — remains open.

### 1.6 The decision: Plan B

A **write-time guard** placed in `BaseTool._instrument_execute` (the same BEFORE-hook that already wraps every tool, via `BaseTool.__init_subclass__` at line 308) can intercept every concrete `BaseTool.execute()` call, decide whether any `output_*` / `frames_dir` / `manifest_path` etc. input would land inside `PROJECTS_DIR`, and:

- if the resolved path is inside `PROJECTS_DIR` but NOT under `PROJECTS_DIR/<project_id>/` or `PROJECTS_DIR/_scratch/<category>/`, return `ToolResult(success=False, error_code="WORKSPACE_CONTRACT")` **without** calling the tool body — i.e., no disk write happens;
- if the resolved path is outside `PROJECTS_DIR` (e.g. `/tmp`, cwd, `~`), the guard is silent — those are not the workspace tree, so they are not the workspace contract's concern;
- also side-channel an `event=workspace_contract_violation` to `events.jsonl` AND `logs/decompose.log` so existing observability (Probe C, Backlot board) can correlate.

This closes the root cause: the next time a script tries `FrameSampler(output_dir="projects")` it returns a structured failure and writes nothing.

### 1.7 Design decisions captured (Section 3 in the plan below)

- **Hard reject, not soft warn** — user explicitly wants prevention, not more alerts.
- **Allow-list = `_scratch/<category>/` only** — CLAUDE.md's documented exception. **`_analysis/` is intentionally NOT allow-listed** even though the directory exists; otherwise today's 24 frames could be quietly hidden there and the guard would say nothing.
- **`OPENMONTAGE_ALLOW_UNSANDBOXED_WRITES=1` env escape hatch** — matches the existing `OPENMONTAGE_PROJECTS_DIR` style in `lib/paths.py:17`. Strict by default, ops can flip off in an emergency.
- **Cover all `BaseTool` subclasses via the existing `__init_subclass__` auto-wrap** — zero per-tool edits, one guard application site.
- **Out of scope**: any non-`BaseTool` Python module that constructs tools and bypasses `.execute()` (a deeper anti-pattern that requires module-import hooks). Not addressed here.

---

## Section 2 — Plan (for sub-agent implementation when approved)

> The plan below is the post-analysis design. It is intentionally concrete
> (with line anchors, sketches, and a verification recipe) so a
> general-purpose sub-agent can execute it without further questions.
> The implementing agent will be a separate run; this document captures
> what to do.

# Active workspace-contract guard (B 方案)

## Context

今天 2026-08-31 13:49 那次事件，24 张 `frame_NNNN.jpg` 文件落到了 `projects/` 根目录，违反了 CLAUDE.md 第 5 条核心不变量（"Tool outputs go under `projects/<project-id>/`"）。**事故根因**在 `tools/analysis/frame_sampler.py:110`：

```python
output_dir = Path(inputs.get("output_dir", input_path.parent / "frames"))
output_dir.mkdir(parents=True, exist_ok=True)   # 立刻创建可写路径
```

如果调用方显式传入 `output_dir="projects"`（或任何最终落到 `projects/` 根的路径），`mkdir(parents=True, exist_ok=True)` 会直接落到 `PROJECTS_DIR/` 根，绕开所有项目命名空间。已经实施的"事后检测"方案（commit `1a77b11`，Probe C 扫 `projects/` 根）**只能事后告警，不能阻止这一笔写入**。

更广义的同类问题：`comfyui_image` / `minimax_video` / `pixabay_image` / `recraft_image` / `video_stitch` / `transcriber` / `video_downloader` 等 ~24 个 `BaseTool` 子类都用同一个模式——`output_path` / `output_dir` 取自 inputs 字符串，没有"必须在 `PROJECTS_DIR/<id>/...` 或 `PROJECTS_DIR/_scratch/<category>/...` 下"的校验。一旦调用方（人或脚本）传错路径，就会落到 `projects/` 根。

本方案：把"输出路径必须在允许目录下"做成 **写前硬守卫**——在 `BaseTool._instrument_execute` BEFORE-hook 拦截所有写盘工具的非法输出路径，**直接返回 `ToolResult(success=False)`**，根本不调内部。涵盖所有 `BaseTool` 子类（零 per-tool 改动）。

### 设计默认（关键决策）

| 决策 | 取值 | 理由 |
|---|---|---|
| 违规时行为 | **硬拒绝**（返回 `ToolResult(success=False, error_code="WORKSPACE_CONTRACT")`） | 13:49 那次就是要"防止"；软告警等于只增加告警，不解决根因 |
| 允许列表 | `_scratch/<category>/`（仅此项，匹配 `projects/_scratch/README.md` 与 CLAUDE.md 不变量 §5 的明确豁免） | `_analysis/` 现存但 CLAUDE.md 未声明它是豁免；为免静默放过 13:49 的隐藏行为，不列入 |
| 环境变量逃生口 | `OPENMONTAGE_ALLOW_UNSANDBOXED_WRITES=1` 关掉守卫（默认启用守卫） | 跟 `OPENMONTAGE_PROJECTS_DIR`（`lib/paths.py:17`）的风格一致；运维紧急时一行可关 |
| 覆盖范围 | 全部 `BaseTool` 子类（一个 hook 应用到所有工具） | 单一入口，不需要逐工具改 |
| 违规时事件 | 同时发 `event=workspace_contract_violation` 到该 tool 的 `projects/<id>/events.jsonl`（按现有 `emit_event` 路径），下游 Probe C 仍能关联告警 | 与现有事件总线兼容 |

---

## Critical files to be modified

### 1. `tools/base_tool.py` — 新增守卫 + 接入 BEFORE-hook

**A. 新增模块级辅助**（放在 `_instrument_execute` 上方，约第 220 行附近）：

```python
# ---------------------------------------------------------------------------
# Workspace-contract write-time guard.
#
# If any string-valued input under _PATH_HINT_KEYS resolves INSIDE PROJECTS_DIR,
# it must also live under either PROJECTS_DIR/<project_id>/... (real project
# directory matching _PROJECT_ID_RE) or PROJECTS_DIR/_scratch/<category>/...
# (documented exception in CLAUDE.md invariant §5).
#
# Tools writing to absolute paths under /tmp, $HOME, cwd, etc. are NOT touched
# — the rule fires only when output would land somewhere inside the workspace
# tree without going through a proper project_id directory.
#
# Disabled by setting OPENMONTAGE_ALLOW_UNSANDBOXED_WRITES=1 in the env.
# ---------------------------------------------------------------------------

_PATH_HINT_KEYS = (
    "output_path", "output_dir", "output_file", "manifest_path",
    "frames_dir", "masks_dir", "rendition_path", "render_path",
)
_PROJECT_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,127}$")
_SCRATCH_TOP = "_scratch"
_ALLOW_ANY_TOP = ()   # intentionally empty: "_analysis" etc. is NOT allow-listed
_WORKSPACE_CONTRACT_ERROR = "workspace_contract_violation"


def _check_workspace_write_permission(inputs: dict[str, Any]) -> Optional[ToolResult]:
    if os.environ.get("OPENMONTAGE_ALLOW_UNSANDBOXED_WRITES") == "1":
        return None
    try:
        projects_root = PROJECTS_DIR.resolve()
    except FileNotFoundError:
        return None  # PROJECTS_DIR doesn't exist (e.g. fresh test env) — no guard
    for key in _PATH_HINT_KEYS:
        v = inputs.get(key)
        if not isinstance(v, str) or not v:
            continue
        try:
            resolved = Path(v).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        try:
            rel = resolved.relative_to(projects_root)
        except ValueError:
            continue  # resolved is outside PROJECTS_DIR — not the guard's concern
        # If we got here, the resolved path IS inside PROJECTS_DIR. Validate.
        parts = rel.parts
        if not parts:
            return _violation_result(resolved, projects_root, reason="projects root")
        top = parts[0]
        if top == _SCRATCH_TOP:
            if len(parts) < 2:
                return _violation_result(
                    resolved, projects_root,
                    reason="projects/_scratch root requires a category subdir",
                )
            continue  # OK: projects/_scratch/<category>/...
        if _PROJECT_ID_RE.fullmatch(top):
            continue  # OK: projects/<valid_id>/...
        return _violation_result(
            resolved, projects_root,
            reason=f"projects/{top}/ is neither a valid project_id nor an allow-listed scratch dir",
        )
    return None


def _violation_result(resolved: Path, projects_root: Path, *, reason: str) -> ToolResult:
    msg = (
        f"workspace_contract_violation: output {resolved} would land at "
        f"{projects_root}/{resolved.relative_to(projects_root).as_posix()} — "
        f"expected either {projects_root}/<project_id>/... or "
        f"{projects_root}/_scratch/<category>/... ({reason}). "
        f"Pass an explicit project_id-keyed path, or set "
        f"OPENMONTAGE_ALLOW_UNSANDBOXED_WRITES=1 to bypass for this run."
    )
    return ToolResult(
        success=False, data={}, artifacts=[],
        error=msg, error_code=_WORKSPACE_CONTRACT_ERROR,
        duration_seconds=0.0,
    )
```

**B. 在 `_instrument_execute`（当前 220-296 行）的 wrapper 内、调用 `tool_execute` 之前**，加入守卫调用（拦截位置设计成**所有现有 start/finish 事件 emit 之前**——违规不应污染 event bus）：

```python
def _instrument_execute(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(self, inputs, *args, **kwargs):
        # NEW (B方案): workspace-contract BEFORE-hook.
        try:
            guard = _check_workspace_write_permission(inputs or {})
        except Exception:
            guard = None   # guard must never break execution
        if guard is not None:
            # Side-channel observability: events.jsonl for Probe C / Backlot
            try:
                from lib.events import emit_event, infer_project_dir
                project_dir = infer_project_dir(inputs or {})
                if project_dir is not None:
                    emit_event(project_dir, {
                        "tool": getattr(self, "name", self.__class__.__name__),
                        "event": "workspace_contract_violation",
                        "error_code": _WORKSPACE_CONTRACT_ERROR,
                        "output_path": str(guard.error),
                        "ts": time.time(),
                    })
            except Exception:
                pass
            try:
                import mcp_server
                mcp_server._decompose_event(
                    "workspace_contract_violation",
                    tool=getattr(self, "name", self.__class__.__name__),
                    inputs={k: str(v)[:200] for k, v in (inputs or {}).items()
                            if k in _PATH_HINT_KEYS},
                    error=str(guard.error)[:300],
                )
            except Exception:
                pass
            return guard
        # existing decoration (start event, fn(), finish event, error event)
        ...
```

注意：
- `import mcp_server` 在 `BaseTool` 上下文里要 lazy（避免重 mcp_server import cost）；失败回退静默。
- `_instrument_execute` 已有的 start / finish / error 三段保持原样，**守卫发生在 start 事件之前**——即违规不会污染 event bus。
- `ToolResult` 的签名沿用现有工具。

### 2. `tools/asset_upload_chunk.py` 已有 `_PROJECT_ID_PATTERN`/`_PROJECT_ID_RE`（约 50-58 行）

直接复用其精确正则（保持项目 ID 校验口径一致），不重新发明：

```python
from tools.asset_upload_chunk import _PROJECT_ID_RE   # 复用，不要重复定义
```

实现代理需先 `grep` 确认无循环引用；若 ImportError 则将 regex 字面值复制到 `base_tool.py`，并在 commit message 里注明"duplicated from tools/asset_upload_chunk.py — keep in sync"。

---

## Files to create

### `tests/regression/test_workspace_contract_guard.py`

6 cases（沿用 `tests/regression/test_mcp_scene_detect_wrapper.py:1-142` 的 lazy-import + `unittest.mock` 模板）：

| # | 用例 | 断言（one-line） |
|---|---|---|
| 1 | `test_guard_rejects_projects_root` | `FrameSampler().execute({"input_path": probe, "output_dir": str(PROJECTS_DIR)})` 返回 `success=False` 且 `error_code == "WORKSPACE_CONTRACT"` |
| 2 | `test_guard_rejects_projects_relative_path` | `output_dir = "projects"`（相对路径解析后落在 `PROJECTS_DIR`）同样被拒 |
| 3 | `test_guard_allows_scratch_subdir` | `output_dir = "<projects>/_scratch/keyframes"` 通过 |
| 4 | `test_guard_allows_real_project_subdir` | `output_dir = "<projects>/<valid_id>/assets/keyframes"` 通过 |
| 5 | `test_guard_ignores_outsider_paths` | `output_dir = "/tmp/xyz"`, `output_dir = "~/.cache/foo"`, `output_dir = "."`（cwd）均不触发守卫 |
| 6 | `test_guard_respects_escape_hatch` | `monkeypatch.setenv("OPENMONTAGE_ALLOW_UNSANDBOXED_WRITES", "1")` 后 `output_dir = str(PROJECTS_DIR)` 不再被守卫 |

Fixtures：每个 case lazy-import `FrameSampler`、`_check_workspace_write_permission`、`PROJECTS_DIR`；用临时 `tmp_path`+`monkeypatch.setenv("OPENMONTAGE_PROJECTS_DIR", ...)` 把 `PROJECTS_DIR` 隔离到 fixture 上，避免脏到真实 `projects/` 根。`tests/integration/conftest.py::_ensure_pythonpath` 的 autouse 风格可直接复用。

### 不需要再新建其他文件

仅 Edit 2 个：`tools/base_tool.py`（新增 helper + 接入 wrapper）+ 上面 1 个测试文件。

---

## Reusable 函数 / 已有表面

| 用什么 | 在哪 | 在本方案中怎么用 |
|---|---|---|
| `BaseTool.__init_subclass__` 自动 wrap | `tools/base_tool.py:308` | 让 BEFORE-hook 应用到全部子类，无需逐工具改 |
| `BaseTool._instrument_execute` | `tools/base_tool.py:225-296` | 在 wrapper 内插 1 个 `if guard: return guard` 即可 |
| `PROJECTS_DIR`（可 env 覆盖） | `lib/paths.py:17` | guard 用 `PROJECTS_DIR.resolve()` 做边界判定；测试可通过 `OPENMONTAGE_PROJECTS_DIR` 隔离 |
| `_PROJECT_ID_RE` 已有正则 | `tools/asset_upload_chunk.py:50-58` | 复用做项目 ID 校验口径（避免与 chunk upload 工具漂移） |
| `emit_event` / `infer_project_dir` | `lib/events.py:46-95` | 违规时发一条 `event=workspace_contract_violation` 进 `events.jsonl`，让 Backlot/Probe C 跨工具关联 |
| `_decompose_event`（A方案刚加的） | `mcp_server.py` 新增段 | 违规时同时落到 `logs/decompose.log`，运维 `tail -F` 直接看到 |
| `tests/integration/conftest.py::_ensure_pythonpath` | `tests/integration/conftest.py` | 复用 autouse 路径设置让测试无需手动 insert sys.path |

---

## Concrete edits（含行锚 + 最小 diff）

### Edit 1 — `tools/base_tool.py` 顶部 imports（约第 1-30 行）

加：
```python
import os
import re
from typing import Optional  # 若现有 typing import 没覆盖
from lib.paths import PROJECTS_DIR
```

如果 `Optional` / `re` / `os` 已经在现有 import 里就跳过；实现代理先 `grep -n '^import\|^from'` 确认。

### Edit 2 — `tools/base_tool.py:220` 附近插入新 helper

按上面 §"1. `tools/base_tool.py` — A" 的代码块插入 `_check_workspace_write_permission` + `_violation_result` + 模块级常量 `_PATH_HINT_KEYS` / `_PROJECT_ID_RE` / `_SCRATCH_TOP` / `_WORKSPACE_CONTRACT_ERROR` / `_ALLOW_ANY_TOP`。位置：紧贴 `_instrument_execute` 上方。

注意：`re.compile` 的字面 regex 与 `tools/asset_upload_chunk.py:50-58` 的 `_PROJECT_ID_RE` **字字相同**——优先 `from tools.asset_upload_chunk import _PROJECT_ID_RE`，避免循环引用；若 ImportError 则内联字面值（duplicated; keep in sync），commit message 加注释。

### Edit 3 — `tools/base_tool.py:225-296` `_instrument_execute` wrapper 接入

按上面 §"1. `tools/base_tool.py` — B" 的代码块插入守卫调用 + 两条 side-channel emit。

精确插入点：现有 wrapper 内 `try` 块之前，所有 start event emit 之前——违规不应产生 start 事件。

### Edit 4 — `tests/regression/test_workspace_contract_guard.py`（新文件）

按上面 §"Files to create" 表的 6 个 case。

---

## Verification

```bash
# 1. 直接尝试 — 守卫启用后应被拒
cd /opt/OpenMontage_Voicebox
python3 - <<'PY'
import os
# 不设 OPENMONTAGE_ALLOW_UNSANDBOXED_WRITES —— 守卫启用
from tools.analysis.frame_sampler import FrameSampler
probe = "demo/sample.mp4"
r = FrameSampler().execute({"input_path": probe, "strategy": "count", "count": 1,
                            "output_dir": "projects"})
print("case A (output_dir='projects'):", r.success, r.error_code, str(r.error)[:120])
assert not r.success and r.error_code == "WORKSPACE_CONTRACT"
PY

# 2. 允许路径应继续工作（用 tmp 隔离 PROJECTS_DIR）
python3 - <<'PY'
import os, tempfile, pathlib
from tools.analysis.frame_sampler import FrameSampler
with tempfile.TemporaryDirectory() as td:
    os.environ["OPENMONTAGE_PROJECTS_DIR"] = td
    from lib import paths as p; p.PROJECTS_DIR = pathlib.Path(td)
    probe = "demo/sample.mp4"
    projects_root = pathlib.Path(td)
    r1 = FrameSampler().execute({"input_path": probe, "strategy": "count", "count": 1,
                                 "output_dir": str(projects_root / "demo-proj" / "assets" / "keyframes")})
    r2 = FrameSampler().execute({"input_path": probe, "strategy": "count", "count": 1,
                                 "output_dir": str(projects_root / "_scratch" / "keyframes")})
    r3 = FrameSampler().execute({"input_path": probe, "strategy": "count", "count": 1,
                                 "output_dir": str(projects_root)})
    print(r1.success, r2.success, r3.success, r3.error_code)
    assert r1.success and r2.success and not r3.success
PY

# 3. /tmp 与 cwd 不受守卫影响
python3 - <<'PY'
import os, tempfile, pathlib
from tools.analysis.frame_sampler import FrameSampler
probe = "demo/sample.mp4"
with tempfile.TemporaryDirectory() as td:
    os.environ["OPENMONTAGE_PROJECTS_DIR"] = td
    from lib import paths as p; p.PROJECTS_DIR = pathlib.Path(td)
    r1 = FrameSampler().execute({"input_path": probe, "strategy": "count", "count": 1, "output_dir": "/tmp/test_frames"})
    r2 = FrameSampler().execute({"input_path": probe, "strategy": "count", "count": 1, "output_dir": "."})
    print(r1.success, r2.success)
    assert r1.success and r2.success
PY

# 4. 逃生口
python3 - <<'PY'
import os, tempfile, pathlib
os.environ["OPENMONTAGE_ALLOW_UNSANDBOXED_WRITES"] = "1"
# 同 case 1: FrameSampler(output_dir="projects") 现在应成功（用 PROJECTS_DIR=tmp 隔离）
PY

# 5. 测试套
python -m pytest tests/regression/test_workspace_contract_guard.py -v   # 6/6
python -m pytest tests/ -v --ignore=tests/integration                    # 1378+ pass，无新回归
```

测试 gate：`tests/regression/test_workspace_contract_guard.py` 必须 6/6 pass；全量 `tests/` 不出新回归（24 个 pre-existing failures 必须仍是同样 24 个，未引入新 fail）。

---

## Out of scope（明确不做的）

1. **不**对 `tools/mcp_health_monitor.py` / `tools/decompose_health_monitor.py` 做修改——本方案只治理**写**端，观察端已是 A 方案的范围。
2. **不**增加 `projects/_analysis/` 到 allow-list：CLAUDE.md 没把它列为豁免，13:49 那 24 张图若继续藏在 `_analysis/` 也会被守卫拒——这是想要的。
3. **不**把守卫扩展到 non-`BaseTool` 模块（即不修 raw 脚本调用 `frame_sampler.FrameSampler.__init__()` 后直接调 `_extract_interval()`）——这种"完全旁路 `execute()`"是更深层的 anti-pattern，需要另一套拦截（如 `frame_sampler` 模块 import 守卫），不在本方案。
4. **不**改 `_PATH_HINT_KEYS` 现有在 `lib/events.py:46-74` 的定义——它服务于事件归属，新 `_PATH_HINT_KEYS`（基于本方案）是 guard 自己独立的小白名单。
5. **不**引入新依赖（新工具 / 新包 / 新 env var 之外的全部）。

---

## 实施委托

`ExitPlanMode` 批准后，dispatch 一个 `general-purpose` Agent（在隔离 worktree 内执行）：

1. Edit 1-3 + 创建 `tests/regression/test_workspace_contract_guard.py`。
2. 跑上面 §Verification 第 5 步；如有 fail，迭代最多 3 次。
3. rebase + ff-merge 到 `release/mvp-v0.1-phase-0-5`（按 A 方案同款流程处理潜在的 base-divergence 工作树分支）。
4. commit message（强制格式，无 Co-Authored-By trailer）：

```
feat(workspace-contract): active write-time guard rejects projects/ root writes

- Add tools/base_tool.py::_check_workspace_write_permission invoked from
  _instrument_execute BEFORE-hook; covers every BaseTool subclass without
  per-tool edits via __init_subclass__.
- Allows output under PROJECTS_DIR/<project_id>/... (matching the project_id
  regex used by tools/asset_upload_chunk.py) and PROJECTS_DIR/_scratch/<category>/...
  (documented exception, CLAUDE.md invariant §5). All other paths inside
  PROJECTS_DIR return ToolResult(success=False, error_code=WORKSPACE_CONTRACT).
- Outputs to /tmp, cwd, $HOME, etc. are unaffected — only PROJECTS_DIR-rooted
  writes are gated.
- Per-violation, side-channel: emit events.jsonl "workspace_contract_violation"
  and logs/decompose.log line, so Probe C (A-plan) and Backlot can correlate.
- Escape hatch: OPENMONTAGE_ALLOW_UNSANDBOXED_WRITES=1 disables the guard
  for ops emergencies (matches the OPENMONTAGE_PROJECTS_DIR style in lib/paths.py).
- Tests: 6 cases in tests/regression/test_workspace_contract_guard.py mirror
  the lazy-import pattern of tests/regression/test_mcp_scene_detect_wrapper.py;
  cover projects-root reject, projects-relative reject, _scratch allow,
  real-project allow, /tmp/cwd ignore, escape-hatch bypass.
```

Operator action（不属本 PR）：把上次 A 方案挂的 24 张 `frame_*.jpg` 此刻仍然在 `projects/` 根——它们是本方案守卫启用前的产物，启用守卫后类似调用会立刻被拒；旧 24 张可以用 `mv projects/frame_*.jpg projects/_scratch/frames-2026-08-31-1349/` 一行归档，与 A 方案设计的"Probe C 5 分钟内告警"一起形成完整闭环。

---

## 与 A 方案关系

A 方案（commit `1a77b11`）：事后看见 `projects/` 根出现挂文件 → Probe C 告警。
B 方案（本文件）：写时直接拒绝，根本不让它落到 `projects/` 根。

两者并存：A 仍是网络层（陌生 automation 走 MCP 时也覆盖），B 是应用层（任何 `BaseTool` 子类调用，覆盖脚本/人/MCP 直连）。A 不需要回退或修改。
