# BGM 工具全景图

> OpenMontage_Voicebox 中所有与背景音乐（BGM）相关的工具一览。
> 包含生成型（AI 生成）和检索型（从曲库/平台搜索现成素材）两类。

---

## 一览表

| 工具 | 类型 | 提供方 | 费用 | 用途 |
|---|---|---|---|---|
| `music_gen` | 生成 | ElevenLabs | 付费 | 视频 BGM / 音效 |
| `music_gen_local` | 生成 | Meta MusicGen | **免费** | 离线 BGM |
| `google_music` | 生成 | Google Lyria 3 | $0.08/次 | 视频 BGM |
| `suno_music` | 生成 | Suno | 付费 | **含人声**完整歌曲 |
| `pixabay_music` | 检索 | Pixabay | **免费** | 免版税曲子 |
| `freesound_music` | 检索 | Freesound | **免费** | 免版税音效/短片段 |
| `music_library` | 检索 | 本地文件 | **免费** | 复用已有素材 |

---

## 工具详解

### 1. `music_gen`（ElevenLabs）⭐ 主路径

- **能力**：生成 BGM + 音效（SFX）
- **模型**：ElevenLabs 内部音乐模型
- **时长**：3–600 秒
- **费用**：$0.05 / 30 秒，付费
- **特点**：
  - `force_instrumental=True`（默认）→ 纯器乐 BGM
  - `force_instrumental=False` → 可生成人声（需显式传参）
  - 也是唯一声明 `generate_sfx`（音效）能力的工具
- **适用场景**：视频默认 BGM，有 ElevenLabs API Key 时优先使用

---

### 2. `music_gen_local`（Meta MusicGen）🆕 本次新增

- **能力**：生成 BGM（纯器乐）
- **模型**：`facebook/musicgen-small`（300M 参数，MIT 代码 + CC-BY-NC 权重）
- **时长**：单次 5–30 秒；>30 秒用 crossfade-loop 拼接
- **费用**：**免费**，完全离线
- **特点**：
  - `force_instrumental=True`（默认，模型本身无 vocal 路径）
  - `force_instrumental=False` → **硬拒绝**，报错指向 `suno_music`
  - `get_status()` 诚实检查权重缓存，不触发静默下载
  - GPU → cuda / mps / cpu 自动检测
  - 输出 WAV（默认）/ MP3（需 ffmpeg）
- **适用场景**：
  - 无 ElevenLabs API Key
  - 网络不可达（生产环境断代理）
  - 预算有限，追求零成本
- **非商用注意**：MusicGen 权重是 CC-BY-NC-4.0，非商用授权。商用项目继续用 ElevenLabs / Suno
- **RFC**：`docs/music-gen-local-rfc-2026-08-28.md`
- **状态**：Draft，2026-08-29 待验收

---

### 3. `google_music`（Google Lyria 3）

- **能力**：生成 BGM（器乐为主）
- **模型**：`lyria-3-pro-preview`
- **时长**：5–184 秒（硬上限）
- **费用**：$0.08 / 次（不按时长）
- **特点**：
  - 支持参考图片生成（visual music conditioning）
  - 超过 184 秒会静默截断（`auto_fix=True` 时）
  - 不支持 SFX
  - `fallback_tools = ["music_gen"]`
- **适用场景**：已有 Google API Key，且 BGM 时长 ≤ 184 秒

---

### 4. `suno_music`（Suno）

- **能力**：生成**含人声/歌词**的完整歌曲
- **模型**：Suno 内部模型
- **时长**：最长 8 分钟
- **费用**：付费（通过 sunoapi.org 按 credits 计费）
- **特点**：
  - 可生成带歌词的 vocal 曲目
  - 器乐也可用（通过 prompt 约束）
  - API 通过 sunoapi.org 调用，需 `SUNO_API_KEY`
- **适用场景**：
  - 需要 BGM 带人声/歌词（片头曲、主题曲等）
  - 与 `music_gen_local` 的 `force_instrumental=False` 拒绝情况互补

---

### 5. `pixabay_music`（免版税检索）

- **能力**：从 Pixabay 免版税音乐库搜索
- **费用**：**免费**（Pixabay API Key）
- **特点**：
  - 不生成，**只检索**已有曲子
  - 按 mood/genre 搜索
  - 直接下载 MP3
- **适用场景**：
  - 不想付版权费
  - 需要现成的免版税背景音乐

---

### 6. `freesound_music`（免版税音效）

- **能力**：从 Freesound 检索音效片段
- **费用**：**免费**（Freesound API Key）
- **特点**：
  - 不生成，**只检索**短音效（SFX）
  - 适合：撞击声、环境音、UI 音效等
- **适用场景**：
  - 需要短音效而非完整 BGM
  - `music_gen` 的 SFX 能力（ElevenLabs）之外的免费替代

---

### 7. `music_library`（本地曲库）

- **能力**：复用项目本地已有音乐文件
- **费用**：**免费**
- **特点**：
  - 从项目 `library/music/` 目录检索
  - 适合：同一项目内多场景复用同一配乐
- **适用场景**：
  - 复用之前生成的 BGM
  - 固定配乐的场景（如系列视频）

---

## 调用链路（Fallback 顺序）

```
生成型 BGM 调用链：

music_gen (ElevenLabs, 付费, 主路径)
    ↓ fallback（无 key / 网络问题）
google_music (Lyria 3, $0.08/次)
    ↓ fallback（无 key / 超184s）
music_gen_local (MusicGen, 免费, 离线, 新增)
    ↓ fallback
UNAVAILABLE → 报错（提示用户加 key 或用 retrieval）

含人声音乐：
suno_music (Suno, 付费, 可 vocal)
    ↔ music_gen force_instrumental=False（ElevenLabs）

检索型（零成本）：
pixabay_music（免费曲子）
    ↔ freesound_music（免费音效）
    ↔ music_library（本地已有素材）
```

---

## 选型建议

| 需求 | 推荐工具 |
|---|---|
| 有 ElevenLabs Key，常规视频 BGM | `music_gen` |
| 无 ElevenLabs Key，离线/生产环境 | `music_gen_local` |
| 有 Google Key，需要 ~3 分钟内 BGM | `google_music` |
| 需要带人声/歌词的曲子 | `suno_music` |
| 免费免版税曲子，不想付版权 | `pixabay_music` |
| 短音效（撞击声/UI音等） | `freesound_music` |
| 复用项目已有配乐 | `music_library` |

---

## 本次新增 `music_gen_local` 的位置

`music_gen_local` 填补的是：

1. **离线可用性**：无网络时 ElevenLabs / Google / Suno 全挂，本机还能跑
2. **零成本兜底**：预算为 0 也能生成 BGM
3. **供应商风险**：不依赖单一 API 服务

不是替代 ElevenLabs（质量差距明显），而是最后一道兜底。

---

*最后更新：2026-08-29*
