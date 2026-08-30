# claude-video × OpenMontage 白名单审核

**Author**: OpenMontage integration reviewer
**Date**: 2026-08-23
**关联文档**:
- [`docs/claude-video-integration.md`](claude-video-integration.md) §5 — 待审核的 pipeline 白名单
- [`/opt/claude-video/docs/todo.md`](../claude-video/docs/todo.md) §2.6.2 — claude-video 侧已 commit 的白名单(同一份)
- [`/opt/claude-video/docs/openmontage-integration-inputs.md`](../claude-video/docs/openmontage-integration-inputs.md) — 给 claude-video 团队的输入清单(F1 typo fix 在那)

---

## 审核方法

不靠"读 pipeline manifest 字面猜",而是真跑:

```python
from tools.tool_registry import registry
registry.discover()
gpu_tools = registry.gpu_required_tools()
# => 任何 resource_profile.vram_mb > 0 的 tool
```

OM 当前注册的 GPU-required tool 全部名单:

| tool 名 | 用途 | 在白名单 pipeline 中出现? |
|---|---|---|
| `cogvideo_video` | 本地 video diffusion | ❌ 不出现 |
| `comfyui_image` | ComfyUI 出图 | ❌ 不出现 |
| `comfyui_video` | ComfyUI 出视频 | ❌ 不出现 |
| `face_restore` | GFPGAN 人脸修复 | ❌ 不出现 |
| `hunyuan_video` | 本地 video diffusion | ❌ 不出现 |
| `lip_sync` | 唇形同步(SadTalker/wav2lip 等) | ⚠️ **仅** `localization-dub.assets.optional_tools` |
| `local_diffusion` | FLUX/SD 本地扩散 | ❌ 不出现 |
| `ltx_video_local` | LTX-Video 本地 | ❌ 不出现 |
| `nllb_translator` | NLLB 本地翻译 | ❌ 不出现 |
| `talking_head` | 数字人头部 | ❌ 不出现 |
| `upscale` | Real-ESRGAN 放大 | ❌ 不出现 |
| `video_understand` | 视频理解模型 | ❌ 不出现 |
| `wan_video` | 本地 video diffusion | ❌ 不出现 |

---

## 逐 pipeline 审核结论

读了每份 `pipeline_defs/<name>.yaml` 里所有 `required_tools` / `optional_tools` / `tools_available`,逐项对照 `registry.gpu_required_tools()`。

| Pipeline | 状态 | 备注 |
|---|---|---|
| `clip-factory` | ✅ **GPU-free** | `required_tools` 只有 `transcriber` / `subtitle_gen` / `video_compose` / `video_trimmer` / `audio_mixer`。可选的 `color_grade` / `audio_enhance` 都不需要 GPU。 |
| `documentary-montage` | ✅ **GPU-free** | `required_tools` 只有 `video_compose`;可选的 `direct_clip_search` / `corpus_builder` / `clip_search` 走 CLIP embedding(CPU 可跑)/ 走 Pexels/Archive.org/NASA 等公共视频源(纯网络);`music_gen` 是 selector,实际 provider 是 cloud-only(`suno_music` 见 `tool_registry.py:339`),不需要本地 GPU。 |
| `podcast-repurpose` | ✅ **GPU-free** | 涉及 `image_selector` / `diagram_gen` / `music_gen` —— **三者均为 selector**,路由到 non-GPU provider。registry.gpu_required_tools() 不会列它们。 |
| `localization-dub` | ⚠️ **GPU-conditional** | `assets` stage 把 `lip_sync` 列入 `optional_tools`。如果用户走"配音 + 唇形同步"模式,会撞上 `lip_sync`(GPU-required)。如果只是"字幕 + 配音",全程 CPU。 |
| `hybrid` | ✅ **GPU-free** | `image_selector` / `video_selector` / `diagram_gen` / `code_snippet` / `tts_selector` / `music_gen` 全是 selector,路由非 GPU provider。`hyperframes_compose` 在 `tools_available` 是兜底选项,默认不会跑。 |
| `screen-demo` | ✅ **GPU-free** | `screen_recorder` / `cap_recorder` / `tts_selector` / `image_selector` / `diagram_gen` 全是 selector 或本地捕获。`hyperframes_compose` 是兜底。 |

### 黑名单匹配度

OM §5 黑名单:
- `local_diffusion` ✅ 在 GPU-required 列表
- `wan_video` / `hunyuan_video` / `cogvideo_video` ✅ 都在 GPU-required
- `_kling` 外部 GPU API —— 通过 `ltx_video_local` / `wan_video` 等本地候选等价物挡掉。仓内 `_kling/` 是 adapter 包,只有在用户配了 API key 才会注册;不需要在本白名单里单独列。
- 隐含应该加入黑名单:`talking_head` / `upscale` / `face_restore` / `video_understand` / `nllb_translator` / `ltx_video_local` / `comfyui_image` / `comfyui_video` —— 这 8 个 GPU-required tool 当前 0 出现在白名单中任何 pipeline 的 tool 列表,所以白名单层面没问题。但建议把它们**显式列在黑名单**里,跟 OM-3 的"防御深度"原则对齐。**这是对原集成文档的一个补丁建议**。

---

## 推荐:Adapter 必须做的硬化

OM 侧的 `tools/external/claude_video.py` 必须在调用 pipeline 之前做:

1. **白名单严格校验**(防御深度第一条):
   ```python
   ALLOWED = {"clip-factory","documentary-montage","podcast-repurpose",
              "localization-dub","hybrid","screen-demo"}
   if inputs.pipeline not in ALLOWED:
       raise ToolError(code="pipeline_not_in_whitelist",
                       message=f"recompose requires GPU-free pipeline; got '{inputs.pipeline}'. "
                               f"Allowed: {sorted(ALLOWED)}")
   ```

2. **`localization-dub` 加 `lip_sync=false` 默认** —— 让这个 pipeline 在 adapter 入口直接写死 `dub_mode="subtitles_only"` 或同等把 `lip_sync` 从可选里关掉。文档 OM-7 已经写了运行时 GPU 检测;**建议在这里再加一个前置守卫**,免得 adapter 等到运行时才发现 GPU 不够。

3. **可选 tool 静态禁用**:adapter 触发 pipeline 时,把 `optional_tools` 透传但同时构造一个 `disabled_tools=["lip_sync"]` 注入 stage 配置,让 stage director 不去调度它们。这比靠模型好心地"不用"更可靠。

4. **错误码表与 claude-video 侧对齐**:详见 `claude-video/docs/openmontage-integration-inputs.md` §2。两边 1:1 同名。

5. **OM-3 自我审计**:每发一版新 pipeline(或者 audit `pipeline_defs/`),跑一次 `registry.gpu_required_tools()` ∩ 新 pipeline 的 `required_tools + optional_tools`,断言为空。这是 CI 加的一个测试用例,300 行内。

---

## 拼写错误(再次提醒)

§5 白名单表里写的是 `podcast-reproduce`,OM `pipeline_defs/` 实际是 `podcast-repurpose`。两边 (`docs/claude-video-integration.md` §4.1 和 §5,`claude-video/docs/todo.md` §2.6.2)都拼错了。**白名单审核部分必须以 `podcast-repurpose` 为准**,这点在 OM 侧 adapter 实现时不容含糊。
