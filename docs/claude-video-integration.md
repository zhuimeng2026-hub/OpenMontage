# claude-video × OpenMontage 集成规格

**Status**: Draft (awaiting OpenMontage owner review)
**Date**: 2026-08-23
**Author**: claude-video team (zhuimeng2026-hub fork)
**claude-video 仓库**: https://github.com/zhuimeng2026-hub/claude-video
**OpenMontage 仓库**: https://github.com/.../OpenMontage_Voicebox (待 owner 补充)
**关联文档**:
- `OpenMontage_Voicebox/docs/web-multiuser-auth.md` — 用户隔离与 OAuth 模型(直接复用)
- `OpenMontage_Voicebox/docs/doc-wechat-open-platform-oauth.md` — 微信 OAuth 流程细节
- `OpenMontage_Voicebox/docs/openmontage-integration.md` — Voicebox × OpenMontage 集成的先例(尤其 §BFF 模式)
- `OpenMontage_Voicebox/docs/comfyui-adapter-plan.md` — 跨仓 adapter 文档结构模板

---

## 1. Motivation

claude-video(`/watch <url> [question]`)当前是一个本地分析工具:下载视频 → 提取帧 → 转录 → 输出 markdown 报告 + 帧路径。它的强项是**对输入视频做精细的结构化分解**,但**不擅长基于分解结果生成新视频**。

OpenMontage 的强项恰好相反:把素材(帧、视频、转录、prompt)按 12 个 YAML pipeline 之一编排,产出最终 `renders/final.mp4`。

集成目标:claude-video 把 `/watch` 的产物(VTT + frames + video)打包成 OpenMontage 可消费的 inputs 包,经 MCP 提交给 OpenMontage;OpenMontage 选合适的 pipeline 跑完整流程,**产物落在 `projects/users/<user_openid>/<project-id>/renders/`**。

---

## 2. 架构

```
┌─────────────────── claude-video 仓库 ───────────────────┐    ┌─────────── OpenMontage_Voicebox ──────────────┐
│                                                          │    │                                              │
│  /watch CLI / start_watch MCP tool                       │    │  mcp_server.py (:8900)                        │
│       │                                                  │    │       │                                      │
│       ▼                                                  │    │       ▼                                      │
│  RunResult { frames/*.jpg, masks/*.png,                  │    │  execute_tool(tool_name="claude_video",      │
│                 transcript_segments[],                   │    │               inputs={...})                  │
│                 work_dir }                               │    │       │                                      │
│       │                                                  │    │       ▼                                      │
│       ▼                                                  │    │  tools/external/claude_video.py (新增)       │
│  session_store.py ──► sessions.json                      │    │       │                                      │
│       │  (video_id, user_openid, work_dir)               │    │       ├─► 创建 projects/users/<openid>/      │
│       │                                                  │    │       │       <project_id>/                  │
│       ▼                                                  │    │       ├─► 拷贝/链接 watch 产物到 assets/     │
│  recompose MCP tool ──────── stdio MCP ──────────────────┼────┼──────►│                                      │
│       │  (video_id, pipeline, style, user_openid)        │    │       ├─► 触发 pipeline (clip-factory /      │
│       │                                                  │    │       │   documentary-montage / ...)         │
│       ▼                                                  │    │       │                                      │
│  Phase 2.7 BFF (FastAPI REST+SSE, :8910)                 │    │       ▼                                      │
│       │                                                  │    │  backlot 自动开启                            │
│       ▼                                                  │    │       │                                      │
│  浏览器 fetch('/api/recompose', ...) ────────────────────┼────┼──────►│ projects/users/<openid>/<id>/             │
│                                                          │    │       │   renders/final.mp4                   │
└──────────────────────────────────────────────────────────┘    │       │                                      │
                                                                  │       └─► SSE / MCP notifications 进度回传   │
                                                                  └──────────────────────────────────────────────┘
```

---

## 3. claude-video 侧要做什么(已在 `docs/todo.md` 跟踪)

- `mcp_server.py` 注册 `recompose(video_id, pipeline, style, user_openid)` tool,内部 stdio 调 OpenMontage MCP 的 `execute_tool`
- 两个 Remotion 脚本(`watch_to_remotion*.py_tmp`)退役,改为 adapter;`OPENMONTAGE_REQUIRED=1` env guard
- `recompose` tool 内置 GPU-free pipeline 白名单,拒绝 `FLUX` / `Kling` / `local_diffusion` / `hunyuan_video` / `wan_video` / `cogvideo_video`
- Phase 2.7 BFF(FastAPI REST + SSE,端口 8910)给浏览器客户端调
- Phase 2.8 微信服务号 OAuth + session 中间件

---

## 4. OpenMontage 侧需要做的代码改动

### 4.1 新增 `tools/external/claude_video.py` BaseTool

模板:参考 `tools/_comfyui/` 或 `tools/_kling/` 的 adapter 风格(BaseTool 子类 + client 库 + 注册到 tool_registry)。

**Tool 名**:`claude_video.compose`(命名空间 `claude_video.*`,避免与现有 `video_compose` 冲突)

**Inputs schema**:
```python
class ClaudeVideoInputs(BaseModel):
    user_openid: str                                  # 微信 openid,用于 projects/users/<openid>/
    project_id: str | None = None                     # 不传则用 video_id
    source: dict                                      # claude-video RunResult 的子集
        # {
        #   "video_id": "abc123def456",
        #   "frames_dir": "/abs/path/to/frames/",     # 包含 frame_NNNN.jpg
        #   "masks_dir":  "/abs/path/to/masks/" | None,
        #   "vtt_path":   "/abs/path/to/transcript.vtt" | None,
        #   "video_path": "/abs/path/to/source.mp4"   | None,
        #   "duration_seconds": float,
        #   "transcript_segments": [{"start":..,"end":..,"text":..}, ...]
        # }
    pipeline: Literal["clip-factory", "documentary-montage",
                      "podcast-reproduce", "localization-dub", "hybrid"]
    style: str = "clean-professional"
    extra: dict = {}                                  # 透传给 pipeline
```

**行为**:
1. 校验 `pipeline` 在白名单内(GPU-free)
2. 解析 `user_openid` 到 `user_id`(MVP 直接当 user_id 用;若以后引入 unionid → user_id 合并逻辑,改这里即可)
3. 创建 `projects/users/<user_openid>/<project_id>/{assets/{frames,masks,video,audio},artifacts,renders}/`
4. **拷贝**(不软链,避免源被清理后失效)frames/*.jpg + masks/*.png + source.mp4 + transcript.vtt 到 `assets/` 对应子目录
5. 在 `artifacts/source_meta.json` 写入 `transcript_segments` + `duration_seconds`
6. 触发对应 pipeline(`pipeline_loader` + `checkpoint.init_project` + 跑 stage directors)
7. 返回 `ToolResult`:
   ```python
   {"status": "submitted", "project_id": ..., "renders_path": ...,
    "backlot_url": f"{base_url}/backlot/{project_id}"}
   ```

**注册**:在 `tools/tool_registry.py` 加 `@register_tool` 或类似装饰器,`agent_skills` 字段指向本文件(`docs/claude-video-integration.md`)。

### 4.2 用户隔离模型

**直接复用 `web-multiuser-auth.md` 的 `projects/users/<user_id>/` 方案**。claude-video 传入的 `user_openid` 落到 `<user_id>`。OpenMontage 不需要重复实现微信 OAuth 流程;claude-video 的 BFF 在浏览器侧跑 OAuth,把 `WATCH_SESSION` cookie 转给 BFF,BFF 在调 OpenMontage MCP 时把 `user_openid` 透传进 inputs。

### 4.3 OAuth 复用

OpenMontage 现有的微信服务号方案(`doc-wechat-open-platform-oauth.md` 实际采用服务号方案,不是开放平台)可以共享 env 变量,但 cookie 名保持独立:
- OpenMontage: `OM_SESSION`
- claude-video: `WATCH_SESSION`
- 两者**共享** `WECHAT_MP_APP_ID` / `WECHAT_MP_APP_SECRET` / `WECHAT_MP_REDIRECT_URI`(若域名重叠)

长期看可以考虑共享 session store(都存 `projects/.users/users.sqlite3`),MVP 不做。

### 4.4 错误返回约定

claude-video 的 `recompose` tool 根据 OpenMontage 返回做映射:

| OpenMontage 错误 | claude-video ToolError |
|---|---|
| `pipeline_not_in_whitelist` | `recompose requires GPU-free pipeline; got "<X>"` |
| `user_not_found` | `OpenMontage rejected user_openid=<...>` |
| `assets_copy_failed` | `failed to copy watch artifacts to <path>` |
| `pipeline_stage_failed` | 透传 OpenMontage 错误,加 `check OpenMontage backlot: <url>` 提示 |

---

## 5. GPU-free Pipeline 白名单

| Pipeline | 备注 |
|---|---|
| `clip-factory` | beta;多片段输出,纯编排,无 GPU 调用 |
| `documentary-montage` | beta;真实素材蒙太奇,纯编排,无 GPU 调用 |
| `podcast-reproduce` | beta;播客高光,纯编排 |
| `localization-dub` | beta;字幕/配音变体 |
| `hybrid` | production;源素材 + 支持性视觉 |
| `screen-demo` | production;屏幕录像 + walkthrough |

### 黑名单(GPU-required,本机不可用)

- `local_diffusion`(FLUX/Stable Diffusion via diffusers)
- `wan_video` / `hunyuan_video` / `cogvideo_video`(本地视频扩散模型)
- 任何 `_kling` / 外部 GPU API 调用

### 验证

claude-video 的 `recompose` tool 在提交前查 `pipeline` ∈ 白名单,违规直接 `ToolError`。OpenMontage adapter 也加第二层校验(防御深度)。

---

## 6. 端到端测试脚本框架

**测试位置**:`OpenMontage_Voicebox/tests/integration/test_claude_video_adapter.py`(如果还没有 `tests/integration/` 目录,参考 `mcp-concurrency-verification-2026-08-19.md` 的组织方式)。

**最小冒烟流程**:
```python
def test_claude_video_adapter_smoke():
    """1) claude-video 跑 /watch → 2) 打包 inputs → 3) 调 adapter → 4) 等 final.mp4"""
    # Step 1: claude-video 端
    from claude_video.mcp_server import run_watch  # 或者 subprocess 调 watch.py
    run_result = run_watch(source="https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4")
    assert run_result.work_dir.exists()
    
    # Step 2: 打包 inputs
    inputs = ClaudeVideoInputs(
        user_openid="test_user_openid",
        source={
            "video_id": run_result.video_id,
            "frames_dir": str(run_result.work_dir / "frames"),
            "video_path": str(run_result.work_dir / "download" / "video.mp4"),
            "vtt_path":   str(run_result.work_dir / "download" / "video.en.vtt"),
            "duration_seconds": run_result.meta["duration"],
            "transcript_segments": run_result.transcript_segments,
        },
        pipeline="documentary-montage",
        style="clean-professional",
    )
    
    # Step 3: 通过 MCP 调 OpenMontage
    result = openmontage_mcp_client.execute_tool(
        tool_name="claude_video.compose",
        inputs=inputs.model_dump(),
    )
    assert result["status"] == "submitted"
    project_id = result["project_id"]
    
    # Step 4: 轮询直到 final.mp4 出现(或者订阅 SSE)
    final = wait_for_render(project_id, timeout_seconds=300)
    assert final.exists()
    assert final.stat().st_size > 0
    assert str(final).startswith(f"projects/users/test_user_openid/{project_id}/renders/")
```

**GPU 防护测试**:
```python
def test_gpu_pipeline_rejected():
    inputs = ClaudeVideoInputs(user_openid="x", source={...}, pipeline="animation")  # animation 可能走 GPU
    with pytest.raises(ToolError, match="GPU-free"):
        adapter.execute(inputs)
```

---

## 7. Issue List for OpenMontage Owner

下面这些项需 OpenMontage owner 接手落地。在 OpenMontage 仓库的 issue tracker 建 epic `claude-video 集成`,子 issue 链接到本文件。

- [ ] **OM-1**:新增 `tools/external/claude_video.py` BaseTool(§4.1 完整 spec)
- [ ] **OM-2**:`tools/tool_registry.py` 注册 `claude_video.compose`(`agent_skills` 指向本文档)
- [ ] **OM-3**:确认 GPU-free pipeline 白名单(§5)与本仓库现有 `pipeline_defs/*.yaml` 一致;若有 pipeline 实际用了 GPU 工具,从白名单移除
- [ ] **OM-4**:在 `web-multiuser-auth.md` 加一节"外部 MCP caller(以 claude-video 为例)"描述 `user_openid` 透传约定
- [ ] **OM-5**:`tests/integration/test_claude_video_adapter.py` 加 §6 冒烟测试
- [ ] **OM-6**:Backlot 在列出项目时,识别 `projects/users/<openid>/` 来源为 `external:claude-video` 的标记(可选,便于审计)
- [ ] **OM-7**:GPU-required pipeline 的运行时检测 —— 若运行时发现 GPU 不可用,自动标记为 `unavailable`,而不是崩

---

## 8. 不做(本集成明确 out-of-scope)

- 共享 OAuth session store(MVP 各自独立跑 OAuth,只共享 env 变量)
- claude-video 直接调 OpenMontage tool registry(只走 MCP,绕过会失去 stage 编排)
- 跨 pipeline 的素材复用(每次 recompose 拷贝产物,不软链)
- OpenMontage 侧改用开放平台(网站应用 ¥300/年)— 保留服务号方案
- 任何 GPU-required pipeline 在本仓库启用
