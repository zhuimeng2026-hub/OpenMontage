# Remotion 多用户并发与数据隔离 — 实现规格

**状态**: 已实现（2026-08-13）— 三个补丁已落地并重新部署生效，见底部「实现记录」
**创建**: 2026-08-13
**目标文件**: `tools/video/video_compose.py`（三个补丁全部落在这一个文件，不碰 `mcp_server.py`）
**相关文档**:
- [`docs/render-queue-analysis.md`](../render-queue-analysis.md) — 渲染队列可行性分析（本设计落地其「第一步」并修正接入点）
- [`docs/stage-gates/PLAN-remotion-asset-staging.md`](../stage-gates/PLAN-remotion-asset-staging.md) — 当前 staging 行为的来源（本设计修复其遗留弱点）
- [`remote-remotion-enable-runbook.md`](../../remote-remotion-enable-runbook.md) — Remotion 端到端验收 runbook（本设计的回归基准）

---

## 1. 背景与问题

`video_compose._remotion_render()` 通过 `npx remotion render`（`subprocess` 原生运行）把 React 合成渲染成 MP4。当前实现有三个真实缺陷，在多用户并行场景下会被放大：

| # | 问题 | 现状（`tools/video/video_compose.py`） | 后果 |
|---|---|---|---|
| P1 | 无并发上限 | MCP 每个渲染 job 起一个 daemon 线程 → `asyncio.to_thread` → `video_compose.execute()`，全程无 semaphore/锁 | N 用户同时渲染 = N 个 `npx remotion render`，每个再开独立 bundler + headless Chrome（~0.5–1GB+ 内存），无界扇出 OOM |
| P2 | staging 全局共享 | `_remotion_render` 把素材拷进唯一目录 `remotion-composer/public/_staged/`（`:1436`），命名 `{idx}_{md5(path)[:8]}_{name}`（`:1644`），从不清理 | 跨用户素材互可见/可加载；hash 的是**路径**不是内容，同路径换内容会撞名；文件无限累积 |
| P3 | 临时文件固定名 | props 写到 `output_path.parent / ".remotion_props.json"`（`:1473`） | 同一 project 两个并发 render 抢同一临时文件 |

本设计三个补丁一一对应解决 P1/P2/P3。

---

## 2. 设计决策汇总

| 决策 | 选择 | 理由 |
|---|---|---|
| 并发闸类型 | `threading.BoundedSemaphore` | `video_compose.execute()` 是同步方法，经 `asyncio.to_thread` 在 **worker 线程**执行。`asyncio.Semaphore` 是事件循环原语，跨线程用是错的；`threading` 原语才是正确选择。`Bounded` 额外能抓「释放次数 > 获取次数」的 bug |
| 闸门位置 | 工具层 `video_compose` 模块级，而非 MCP 层 | 见 §2.1，封住所有调用方且紧贴真正的重资源出口 |
| 并发上限默认值 | `2`，`REMOTION_MAX_PARALLEL` 可覆盖 | 每次 render 内部还有 `--concurrency min(cores,8)`（`:48`），2 个并行已是 ~16 线程 + 2 个 Chrome |
| staging 隔离 | 每次 render 一个 `public/_staged/<staging_id>/` 子目录 | 目录级隔离，跨用户/跨 render 互不可见 |
| `staging_id` 来源 | `inputs.get("staging_id")`，缺省 `uuid4().hex[:12]` | 调用方可传 MCP 的 `job_id` 便于日志关联；缺省自给自足 |
| 命名 hash | **内容** md5（流式），非路径 | 不同文件即使同名也不撞；内容相同自动去重 |
| 拷贝方式 | 单遍「边拷边 hash」+ 原子落名 | 大视频（背景/OffthreadVideo 源）不读两遍 |
| staging 清理 | `finally` 里 `shutil.rmtree(staged_dir)` | 渲染完素材即无用，杜绝跨用户累积 |
| props 文件名 | `.remotion_props.<staging_id>.json` | 同 project 并发/retry 不撞 |

### 2.1 为什么把闸门从 MCP 层下移到工具层（修正 `render-queue-analysis.md`）

`render-queue-analysis.md` 第一步建议在 `mcp_server.execute_tool()` 加 `asyncio.Semaphore(N)`。但自那之后架构已变，该建议现在**覆盖不到渲染**：

1. `create_remotion_video_share`（`mcp_server.py:542`）**立即返回** `status="queued"`，真正的 render 在 `_run_render_job`（`mcp_server.py:653`）的 **daemon 线程**里 `asyncio.run(_worker())` 执行。`execute_tool()` 早已返回，那里的信号量管不到后台线程。
2. `video_compose` 经 `_run_tool_sync`（`mcp_server.py:679`）→ `asyncio.to_thread` 在 worker 线程同步执行，必须用 `threading.Semaphore`。
3. 闸门放在 `_remotion_render`（唯一重资源出口）能封住**所有**调用方（MCP、直接 pipeline、测试脚本），不只 MCP 一条路。

结论：本设计是 `render-queue-analysis.md`「第一步」的**修正与落地**。其「第二步」（`lib/scheduler.py` 资源感知调度器）不变，仍是后续根治方向，见 §7。

---

## 3. 补丁规格

所有行号以当前 `tools/video/video_compose.py` 为准。三处改动互相独立，可分别 review/合入。

### 补丁 1 — 全局并发上限

**改动 1a**：模块级 import，`subprocess` 之后加 `threading`（现 `:23–:30` 区间）：

```python
import json
import logging
import subprocess
import threading
import time
```

**改动 1b**：在 `_get_remotion_concurrency()`（`:48–:63`）之后新增两个模块级 helper：

```python
_REMOTION_RENDER_SLOTS = None  # 惰性创建的进程级 BoundedSemaphore


def _get_remotion_max_parallel() -> int:
    """并发 Remotion render 进程上限。

    每个 `npx remotion render` 都会在 --concurrency 线程池之上再起一个独立
    bundler + headless Chrome（约 0.5~1GB+ 内存）。N 个用户同时渲染 → N 个
    Chrome，无界扇出会 OOM 宿主。默认 2，可用 REMOTION_MAX_PARALLEL 覆盖。
    """
    env_val = os.environ.get("REMOTION_MAX_PARALLEL")
    if env_val:
        try:
            val = int(env_val)
            if val >= 1:
                return val
        except ValueError:
            pass
    return 2


def _get_remotion_render_semaphore() -> threading.BoundedSemaphore:
    """返回进程级 Remotion 渲染闸门。"""
    global _REMOTION_RENDER_SLOTS
    if _REMOTION_RENDER_SLOTS is None:
        _REMOTION_RENDER_SLOTS = threading.BoundedSemaphore(_get_remotion_max_parallel())
    return _REMOTION_RENDER_SLOTS
```

> 说明：`os` 已在模块级导入（`:45`），可直接用。`from __future__ import annotations`（`:23`）使返回注解惰性求值，无需额外 import。

**改动 1c**：把 `_remotion_render` 里包住 `run_command` 的 `try/except/finally`（现 `:1529–:1568`）整体包进 `with sem:`，并在进入前/后各发一条进度（SSE 借此显示「排队→渲染」）。完整「改动后」视图见 §4。

### 补丁 2 — 按 job 隔离 staging + 内容 hash + 用后即清

**改动 2a**：把全局目录改成 per-job 子目录（现 `:1436`）：

```python
# 改前
        staged_dir = composer_dir / "public" / "_staged"

# 改后
        import uuid
        # 每次 render 一个独立 staging 子目录，杜绝跨用户素材互相可见/碰撞。
        # 调用方可传 staging_id（如 MCP 的 job_id）便于日志关联。
        staging_id = inputs.get("staging_id") or uuid.uuid4().hex[:12]
        staged_dir = composer_dir / "public" / "_staged" / staging_id
```

> 后面 `for idx, cut in enumerate(...)` 的 staging 循环与 `audio` 段**不改**——它们已经通过 `staged_dir` 形参传递，会自动落到新子目录。`staging_id` 需在补丁 3 的 props 命名处复用，故定义位置要在两者之前（`:1436` 满足）。

**改动 2b**：重写 `_stage_remotion_asset`（现 `:1625–:1648`）为「单遍拷贝 + 内容 hash + 原子落名 + 返回带 job 子目录的相对路径」：

```python
    def _stage_remotion_asset(self, source: str, idx: int, staged_dir: Path) -> str:
        """把一个本地素材拷进 per-job staging 目录，返回 public 相对路径。

        http/https/data: 直通；文件缺失直通。目标名内嵌**内容** hash（流式
        md5，单遍边拷边算，大视频不读两遍），所以不同文件即使同名也不撞；
        staged_dir 本身按 job 隔离，跨用户/跨 render 互不可见。
        """
        if not source or source.startswith(("http://", "https://", "data:")):
            return source
        resolved = Path(source.replace("file://", ""))
        if not resolved.exists():
            return source
        import hashlib
        import os as _os
        import tempfile

        staged_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.md5()
        # 用 mkstemp 拿唯一临时名，避免多字段共用 idx（如 narration/music 都是
        # idx=-1）时临时文件撞名；`os.replace` 保证同目录内原子落名。
        fd, tmp_name = tempfile.mkstemp(prefix=f".{idx}.", suffix=".tmp", dir=staged_dir)
        try:
            with resolved.open("rb") as src, _os.fdopen(fd, "wb") as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(chunk)
                    dst.write(chunk)
        except OSError:
            try:
                _os.unlink(tmp_name)
            except OSError:
                pass
            raise
        target = staged_dir / f"{idx}_{digest.hexdigest()[:12]}_{resolved.name}"
        if target.exists():
            _os.unlink(tmp_name)  # 同 job 内重复引用 → 去重
        else:
            _os.replace(tmp_name, target)
        # 返回 public 相对路径：_staged/<job_id>/<name>，staticFile() 可直接加载
        return (Path(staged_dir.parent.name) / staged_dir.name / target.name).as_posix()
```

**改动 2c**：在 `finally`（现 `:1558–:1568`）里，删 props 之外再 `rmtree` 整个 per-job staging 目录。完整视图见 §4。

### 补丁 3 — 临时 props 文件名带 job 标识

**改动**（现 `:1473`）：

```python
# 改前
        props_path = output_path.parent / ".remotion_props.json"

# 改后
        props_path = output_path.parent / f".remotion_props.{staging_id}.json"
```

`staging_id` 已在补丁 2a 定义且作用域覆盖到此处。

---

## 4. 改动后完整视图（`_remotion_render` 三个区域）

实现模型可直接对照这三段落地，消除歧义。

### 4.1 staging 段（对应现 `:1430–:1475`）

```python
        composer_dir = Path(__file__).resolve().parent.parent.parent / "remotion-composer"
        if not composer_dir.exists():
            return ToolResult(
                success=False,
                error=f"Remotion composer project not found at {composer_dir}",
            )
        import uuid
        staging_id = inputs.get("staging_id") or uuid.uuid4().hex[:12]
        staged_dir = composer_dir / "public" / "_staged" / staging_id

        for idx, cut in enumerate(props.get("cuts", [])):
            for field in ("source", "backgroundImage", "backgroundVideo"):
                if cut.get(field):
                    cut[field] = self._stage_remotion_asset(cut[field], idx, staged_dir)
            if cut.get("images"):
                cut["images"] = [
                    self._stage_remotion_asset(img, idx, staged_dir)
                    for img in cut["images"]
                ]

        audio = props.get("audio")
        if audio:
            for layer in ("narration", "music"):
                if audio.get(layer, {}).get("src"):
                    audio[layer]["src"] = self._stage_remotion_asset(
                        audio[layer]["src"], -1, staged_dir
                    )

        # （themeConfig 派生逻辑保持原样，不动）

        # Write props to temp file for Remotion CLI
        props_path = output_path.parent / f".remotion_props.{staging_id}.json"
        with open(props_path, "w", encoding="utf-8") as f:
            json.dump(props, f)
```

### 4.2 闸门 + run_command 段（对应现 `:1529–:1568`）

```python
        # 闸门：限制并发 Remotion 进程数，防止多用户无界扇出打爆宿主。
        sem = _get_remotion_render_semaphore()
        if progress_callback:
            try:
                progress_callback({"phase": "render", "status": "queued",
                                   "message": "等待可用的 Remotion 渲染槽位"})
            except Exception:
                pass
        with sem:
            if progress_callback:
                try:
                    progress_callback({"phase": "render", "status": "rendering",
                                       "message": "已获取 Remotion 渲染槽位"})
                except Exception:
                    pass
            try:
                # Invoke from inside the composer dir so npx can resolve the
                # local remotion binary via node_modules/.bin. Without this,
                # Windows npx cannot locate the CLI and returns "could not
                # determine executable to run".
                on_output = None
                if progress_callback:
                    on_output = lambda line: self._emit_remotion_progress(line, progress_callback)
                self.run_command(cmd, timeout=subprocess_timeout, cwd=composer_dir, on_output=on_output)
            except subprocess.CalledProcessError as e:
                detail = (e.stderr or e.stdout or "").strip()
                tail = "\n".join(detail.splitlines()[-25:]) if detail else "(no output captured)"
                return ToolResult(
                    success=False,
                    error=f"Remotion render failed (exit {e.returncode}):\n{tail}",
                )
            except subprocess.TimeoutExpired as e:
                return ToolResult(
                    success=False,
                    error=(
                        f"Remotion render timed out after {e.timeout}s. If the headless "
                        "browser is slow to start, raise remotion_timeout_ms (ms)."
                    ),
                )
            except Exception as e:
                return ToolResult(success=False, error=f"Remotion render failed: {e}")
            finally:
                # Best-effort cleanup of the temp props file. A failed deletion
                # (e.g. host file-protection hooks intercepting unlink) must NEVER
                # abort an otherwise successful render, so swallow any error here.
                try:
                    if props_path.exists():
                        props_path.unlink()
                except OSError as e:
                    logging.getLogger("video_compose").warning(
                        "Could not remove temp props file %s: %s", props_path, e
                    )
                # 清理 per-job staging 目录——渲染完成后素材即无用，留着会跨用户累积。
                try:
                    shutil.rmtree(staged_dir, ignore_errors=True)
                except OSError:
                    pass
```

> 注意：`shutil` 在 `_remotion_render` 顶部已 `import shutil`（现 `:1388`）。若实现时发现缺，补局部 `import shutil` 即可。

---

## 5. 边界条件与已知限制

1. **信号量是进程级的**。当前 MCP 是单 systemd 进程（见 git log「单副本会话一致性」），单进程下全局生效。若将来用 gunicorn 多 worker 拉起，每个 worker 各自一个独立上限——那是升级到「统一队列 + worker 池」的信号（§7），不是本补丁的职责范围。
2. **同 output 的并发 render 仍不隔离**。两个 render 写同一 `output_path`（同 project 同目标）本来就冲突（输出文件互踩），per-job staging 只解决素材隔离，不解决同一输出目标的竞态。调用方应保证 `output_path` 唯一。
3. **内容 hash 对超大视频**是流式单遍（1MB 块），不额外读整文件；只有「同 job 内同一文件被引用两次」时才触发一次去重读，可接受。
4. **`_staged/<staging_id>/` 仍受 `.gitignore:74`（`remotion-composer/public/*`）覆盖**，不会污染 VCS，无需改 `.gitignore`。
5. **`staging_id` 由调用方传入时**，应保证其值不含路径分隔符（`/`、`..`），避免目录穿越。当前 MCP 未传，缺省走 `uuid4`；若将来接入 `job_id`，需在下层做一次合法性校验（`job_id` 由服务端生成，风险低，但建议加注释提醒）。
6. **`mkstemp` 生成的文件权限是 0600**。渲染 subprocess 与 `video_compose` 同一用户，`staticFile()` 由同进程 bundler 服务，0600 可读，无影响；若将来 render 进程以不同用户运行，需改权限或改用 `shutil.copy2` 语义。

---

## 6. 测试与验收计划

实现后按顺序验证，全部通过才算完成。

### 6.1 单元级（`_stage_remotion_asset`）

在 `tests/` 下加（或复用现有 `tests/qa/` 约定）：

1. **内容命名**：两个不同内容的文件同名（如不同目录各一个 `image.png`）→ 生成两个不同 staged 文件名，互不覆盖。
2. **去重**：同一文件 staged 两次 → 只落一个 target，两次返回相同路径。
3. **直通**：`http://…`、`data:…`、不存在的本地路径 → 原样返回。
4. **返回路径**：返回值为 `_staged/<staging_id>/<name>` 形式（含子目录）。

### 6.2 并发上限

1. `export REMOTION_MAX_PARALLEL=2`，并发提交 4 个 render，观察 `pgrep -fc "remotion render"` 峰值 ≤ 2，其余排队。
2. 进度回调收到 `status="queued"` → `status="rendering"` 的转换。
3. 把 `REMOTION_MAX_PARALLEL` 设为 1，验证串行化（峰值 = 1）。

### 6.3 数据隔离与清理

1. 两个不同 project 并发 render，断言各自素材落在不同 `_staged/<id>/` 子目录，且 props 中不出现对方的 `_staged/<id>/` 路径。
2. render 结束后 `_staged/<id>/` 目录被删除（`rmtree` 生效）。
3. `.remotion_props.<staging_id>.json` 用后删除。

### 6.4 回归

1. 重跑 [`remote-remotion-enable-runbook.md`](../../remote-remotion-enable-runbook.md) §8 的单图 Ken Burns 冒烟：`success: true`、`operation: remotion_render`、`silent_downgrade_detected: false`、`runtime_swap_detected: false`。
2. `ffprobe` 输出 h264 / 目标分辨率 / 30fps。
3. 背景图/背景视频/音频字段（`backgroundImage`/`backgroundVideo`/`audio.*`）仍能正确 staging 并出片（对应 `PLAN-remotion-asset-staging.md` 的验收项）。

---

## 7. 后续路线（本补丁之后，另行立项）

1. **资源感知调度器**：`render-queue-analysis.md` 第二步 —— 新增 `lib/scheduler.py`，消费已声明未使用的 `ResourceProfile`，把「固定并发上限」升级为「按 CPU/内存准入 + FIFO 排队 + 队内位置」。本补丁的 `threading.Semaphore` 是其最小前身，接口可平滑迁移。
2. **统一两套 Remotion 运行时**：当前并存 OpenMontage 内置 `npx` 路径（本补丁改的）与独立 `remotion-server`(4000)+Redis/BullMQ 的 HTTP 渲染栈（见 runbook）。多用户化前应收敛成一套「统一队列 + per-job workspace + 有界 worker 数」。
3. **Docker worker 池**：当并发用户量超过单机 semaphore 能平滑兜住、或安全隔离成为硬需求（渲染不可信用户提交的 composition）时，上「固定大小常驻 worker 容器池」而非「每 job 起容器」。数据分离靠 per-job workspace + 对象存储，不靠每用户一容器。

---

## 实现记录（2026-08-13）

三个补丁全部实现、验证并重新部署：

**改动文件**
- `tools/video/video_compose.py` — 补丁 1/2/3
- `tests/tools/test_remotion_staging.py` — 适配 per-job 子目录契约，新增 `test_return_path_includes_job_subdir`
- `tests/qa/smoke_remotion_staging.py` — staging 后检查改为「无残留 per-job 子目录」+「遗留扁平文件仅告警」

**验证结果**
- 单元/契约测试：`test_remotion_staging` 7 项 + 相关 17 项全过；`tests/tools/` + workbuddy 294 过；全量单元 380 过、1 跳过；`tests/contracts/` 684 过、7 跳过
- 并发验证：默认 2、`REMOTION_MAX_PARALLEL=3` 覆盖生效、5 worker@cap2 峰值活跃 ≤2
- 冒烟渲染（`tests/qa/smoke_remotion_staging.py`，真实 headless Chrome）：`success: True`，video+audio 流，渲染后无残留 per-job 目录
- 清理了旧全局 staging 的 60 个扁平残留文件（`remotion-composer/public/_staged/`）

**部署**
- 无编译步骤（Python）。`systemctl restart openmontage-mcp.service`（`WorkingDirectory=/opt/OpenMontage`，pyenv 3.11.8）
- 预检：生产解释器导入新代码 OK，`render_engines: {ffmpeg: true, remotion: true, hyperframes: true}`
- 重启后线上验证：8900 监听正常，startup 日志干净，孤儿恢复 15 job 索引，MCP 握手成功，`get_provider_menu` 三运行时全 `true`
