# animated-explainer — Capability Gaps & Upgrade Paths

> 上面 `environment-check` 里说"video_generation 1/24 是关键瓶颈"——这份专门把 gap 列出来，给"要不要补"的决策用。

## Gap Matrix

| Gap | 影响 | 严重性 | 补法 | 成本 |
|---|---|---|---|---|
| video_generation provider 数量 (1/24) | cinematic 视觉出不来 | **High** | 加 Kling/VEO/Runway | $0.05-0.50/段 |
| image_generation 风格多样性 (3/13) | 视觉风格单调 | Medium | 加 Flux/Imagen/Recraft | $0.05-0.10/图 |
| edge_tts unavailable | 中文 TTS 质量打折 | Medium | `pip install edge-tts` | $0 |
| music_generation 1/4 | bgm 质量一般 | Low | 加 Suno/ElevenLabs | $0.50/首 |
| avatar 0/4 | 数字人讲解做不了 | N/A for EDC | 不补 |
| source_ingest 0/1 | 输入 URL 下载做不了 | N/A (本地 file 已在) | 不补 |
| music_library 0/1 | 没有预设曲库 | Low | 用户手动放 mp3 到 `music_library/` | $0 |
| corpus_population 0/1 | 没有内容语料 | N/A for EDC | 不补 |
| 8GB GPU | comfyui 高显存 workflow 跑不动 | Medium | 升 GPU 16GB / 写低显存 workflow | 硬件升级 |

## 给"出片级" animated-explainer 准备的最小升级包

如果你未来想真的拿 animated-explainer 出片，建议装这一组 env var（按 ROI 排序）：

### 1 步到位（5 秒装好，免费）
```bash
pip install edge-tts
# 重启 mcp_server 让 registry 重新 discover
```
解锁：edge_tts 变 available，TTS 能力 3/10 → 4/10。

### 2 Kling API（10 秒装好，便宜）
```bash
echo 'KLING_API_KEY=your_key' >> /opt/OpenMontage_Voicebox/.env
# 重启 mcp_server
```
解锁：video_generation 1/24 → 2/24，video + image 同时升级（kling_official 也是 image provider）。

### 3 Flux API（10 秒装好，中等）
```bash
echo 'BFL_API_KEY=your_key' >> /opt/OpenMontage_Voicebox/.env
# 重启 mcp_server
```
解锁：image_generation 3/13 → 4/13，FLUX 视觉质量显著高于现有 3 个。

### 4 Suno API（10 秒装好，便宜）
```bash
echo 'SUNO_API_KEY=your_key' >> /opt/OpenMontage_Voicebox/.env
```
解锁：music_generation 1/4 → 2/4。

合计 4 步 / 35 秒 / ~$0.50-2.00/视频（按 1 video + 30 image + 1 music 估算）。解锁后 animated-explainer 出片质量档从"stock-image" → "indie cinematic"。

## 不要补的

- `azure_stt` —— 当前 `faster-whisper` 本地已够用
- `avatar` (kling_official sadtalker wav2lip) —— animated-explainer 默认不产生数字人主播，avatar 是 `talking-head` pipeline 才用
- `source_ingest` —— 当前有本地源文件就够了；真要"输入 URL 下载"另说
- `corpus_population` —— research stage 不需要外部语料
- `music_library` —— 可以手动放 mp3 到 `music_library/`，不需要注册 provider
