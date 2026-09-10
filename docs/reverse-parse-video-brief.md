# 反向解析视频 Brief 模板

> **用途**：把任意已渲染成片的 OpenMontage 项目反向拆解成可复刻的"业务场景描述模板"，
> 让运营/品牌/创作者下次只需要按模板填空，就能让 agent 跑出同等品质的成片。
>
> **起源项目**：`projects/xiaohongshu-neon-night-30s/`（小鹏哥 · 霓虹潮牌单品亮相 30s，2026-09-09 → 2026-09-10）。
>
> **写作方式**：本模板是经过一次完整 cinematic pipeline 跑通后"反向切片"出来的——
> 从 `checkpoint_proposal → checkpoint_script → checkpoint_scene_plan → checkpoint_edit → checkpoint_assets → renders/`
> 全链路读完后，归纳出"什么样的 brief 能生成这样的成片"。

---

## 一、原项目 30 秒成片"反向切片"（以小鹏哥为例）

| 字段 | 反向推回的内容 |
|---|---|
| **品牌主体** | 「小鹏哥」(中文潮牌名，干净无衬线字体 + 微 cyan 发光) |
| **品类** | 潮牌单品（一件衣服 / 黑色单品） |
| **目标平台** | 小红书（次优 TikTok；竖屏 9:16） |
| **目标受众** | 18–30 潮牌爱好者 / Z 世代 / 偏好 cinematic 短平快产品视频 |
| **总时长** | 30 秒 = 5 段 × 6 秒（每段一次 I2V 生成） |
| **核心视觉语言** | 雨夜 + 湿沥青反射 + 霓虹招牌 spillover + Blade Runner 2049 街头调色 |
| **节奏曲线** | stillness（0-6s 空街）→ buildup（6-12s 单品入画）→ reveal（12-18s 慢推 dolly-in）→ intimacy（18-24s 特写）→ brand impact（24-30s 落版） |
| **声音设计** | 无旁白；Pixabay CC0 cyberpunk synthwave 配乐（Brain Implant by VasilYatsevich），t=24.5 drop beat 砸品牌卡 |
| **字幕策略** | 3 张 title card（Noto Sans CJK SC）：相机往前推 / 夜色是新的纹理 / 小鹏哥；后置品牌揭示 拉 watch-through |
| **渲染栈** | cinematic pipeline + Remotion 合成 + 35mm grain + film vignette + DOF focus rack |
| **预算** | $2.0；Hailuo-2.3-Fast（I2V 6s/clip × 5 段）；中途 2067 配额耗尽只跑出 3 段 + 18s 残片 |

---

## 二、用户原始 brief 应该长什么样

下面是**能让 agent 复刻同款**的业务场景描述（中文版）：

> **【业务背景】**
> 我是潮牌「小鹏哥」的内容运营，最近要在小红书发一条 30 秒的"新品单品亮相"短视频。
>
> **【目标】**
> 给这件黑色潮牌单品（卫衣/夹克）做一条 cinematic 风格的产品发布视频，让 Z 世代潮人看了觉得"高级、不土、能转评赞"。
>
> **【不要什么】**
> - 不要用真人模特、不要 walking-away 剪影——这是 2024 已经饱和的拍法；
> - 不要把品牌名贴 logo 在画面最显眼的位置拉低 hook；
> - 不要硬切快剪 + drop beat 一秒一刀的 hype edit，看着累。
>
> **【想要什么】**
> - **场景**：雨后的湿沥青街道，没人，霓虹招牌（青/紫/粉）在背后把光泼在水洼里，单品就静静地躺在画面正中央。
> - **运镜**：从远端慢推 dolly-in 到单品特写，电影感 35mm 颗粒 + 浅景深 + 暗调 lighting。
> - **节奏**：前 6 秒留白（空街 + 霓虹 spillover 钩子）→ 6-12s 单品入画 → 12-18s 慢推看见面料纹理 → 18-24s 极近景看织物质感 + 霓虹 rim light → 24-30s 「小鹏哥」三个字居中落版（带一点 cyan glow）。
> - **声音**：纯画面 + cyberpunk synthwave 配乐 + t=24.5 一个 drop beat 砸品牌卡；**不加任何旁白**。
> - **字幕**：节奏点上插三张中文短句——"相机往前推。光给纹路以形状。"、"夜色是新的纹理。"、"小鹏哥"。
> - **音乐版权**：用 CC0 音乐，别用有版权的 cyberpunk 合成器曲。
> - **品牌字体**：Noto Sans CJK SC 干净无衬线（不要宋体/手写体）。
>
> **【交付规格】**
> 9:16 竖屏 1080×1920，30 秒成片，单个 MP4 ≤ 10MB。
>
> **【预算与约束】**
> 总预算 ≤ $2 USD；可用 minimax Hailuo-2.3-Fast 做 I2V 6 秒/段（注意：本次主机 Token Plan 跑 5 段会撞 2067 配额上限，需要复用前段 first_frame + 中途若撞墙就暂停等重置）。

---

## 三、把这段 brief 给 OpenMontage agent 后，它实际跑了什么

为了让你看到 agent 在背后做了什么决定，对照下表：

| Agent 阶段 | 关键决策 | 输出文件 |
|---|---|---|
| **research** | 调研"小红书 潮牌 cinematic 30s" 的供给缺口，发现 wet-asphalt + neon-spillover 是 2024-2026 的空档 | `artifacts/research_brief.json` |
| **proposal** | 给你 4 个概念候选（Wet Neon / POV 橱窗 / 剪影 / 3 层叠化），你勾了 c1 Wet Neon，Hailuo-2.3-Fast（不挤 H3） | `artifacts/proposal_packet.json` + `decision_log.json` |
| **script** | 5 段 title-led cinematic 脚本，每段一行中文画面提示，不写旁白 | `artifacts/script.json` |
| **scene_plan** | 5 个 scene，全部 type=generated（Hailuo I2V），scene-5 设 hero_moment=true | `artifacts/scene_plan.json` |
| **assets** | 用 minimax_image 生成 5 张 first_frame（墨绿夜色 + 雨后湿光），再用 minimax_video_direct I2V 各跑一段 6s 视频；前 3 段成功，第 4 段撞 2067 配额 | `artifacts/asset_manifest.json` 14/16 生成 |
| **edit** | 硬切 + 淡入淡出；3 张 title card overlay；cyberpunk BGM 48s 截 30s | `artifacts/edit_decisions.json` |
| **compose** | Remotion CinematicRenderer 合成 partial 18s 残片 | `renders/partial_18s_v1.mp4` (5.14MB) |

---

## 四、什么时候 agent 不会自动做、需要你拍板

为了让你下次遇到类似的 brief 时知道在哪里插入"人"：

1. **概念方向选择**（proposal 阶段，guided policy）：agent 会问"4 个候选你选哪个"。
2. **首次 sample 试片**（script 阶段）：agent 会先花 $0.003 跑 5 秒 sample，看视觉对了再让你点头放大到 30s。
3. **预算撞墙**（assets 阶段）：第 N 段 Hailuo 撞 2067 配额时，agent 会停下来 AskUserQuestion 让你二选一：
   - 升级 Token Plan
   - 暂停等下小时重置
   - 改用 ffmpeg-animatic 占位
4. **品牌落版的字体/位置**：你 brief 里不写，agent 默认 Noto Sans CJK SC + 居中 + 微 cyan glow（这是当前项目里你 approve 的版式）。

---

## 五、可复用 Brief 模板（替换占位符即可）

下次直接复制这段模板，把方括号 `[…]` 部分替换掉，就能稳定复刻：

```
【品牌】[品牌名]（中文 / 字体偏好，例如 Noto Sans CJK SC 干净无衬线）
【品类】[潮牌 / 美妆 / 餐饮 / 数码 / 食品] [单品类型]
【目标平台】[小红书 / TikTok / 视频号 / Instagram Reels]
【目标受众】[年龄段] + [人群画像] + [审美偏好关键词]
【总时长】[15 / 30 / 60] 秒 = [N] 段 × [M] 秒（每段一次 I2V 生成）
【核心视觉语言】[主场景一句话] + [光线] + [质感关键词]（例：雨夜 / 湿沥青 / 霓虹 spillover / Blade Runner 街头调色）
【节奏曲线】[情绪起点] → [情绪过程] → [情绪过程] → [情绪过程] → [落版情绪]
【运镜】[推/拉/摇/移/固定] + [速度] + [焦段变化] + [DOF / grain / vignette 等电影感元素]
【声音】[有/无旁白] + [音乐类型/曲风] + [节奏点 drop beat 时刻]
【字幕】[N] 张 title card + [每张出现时刻 + 字体] + [品牌落版时机]
【规格】[9:16 / 16:9] + [分辨率] + [单 MP4 大小上限]
【预算】$X USD
【约束】避免：[饱和套路] / [风险提示]
【首选视频模型】[minimax Hailuo-2.3-Fast / H3 / Seedance / Sora]（注意本机配额上限）
```

### 模板填示例（潮牌单品类）

```
【品牌】小鹏哥（Noto Sans CJK SC 干净无衬线）
【品类】潮牌单品（一件黑色卫衣/夹克）
【目标平台】小红书（次优 TikTok）
【目标受众】18-30 潮牌爱好者 / Z 世代 / 偏好 cinematic 短平快产品视频
【总时长】30 秒 = 5 段 × 6 秒（每段一次 I2V 生成）
【核心视觉语言】雨后湿沥青 + 霓虹招牌 spillover + Blade Runner 2049 街头调色
【节奏曲线】stillness → buildup → reveal → intimacy → brand impact
【运镜】从远端慢推 dolly-in 到单品特写 + 35mm 颗粒 + 浅景深 + 暗调 lighting
【声音】无旁白 + cyberpunk synthwave 配乐 + t=24.5 drop beat 砸品牌卡
【字幕】3 张 title card：相机往前推 / 夜色是新的纹理 / 小鹏哥（后置品牌揭示）
【规格】9:16 竖屏 1080×1920，30 秒成片，单 MP4 ≤ 10MB
【预算】$2 USD
【约束】避免真人模特 / walking-away 剪影 / 一秒一刀 hype edit
【首选视频模型】minimax Hailuo-2.3-Fast（5 段会撞 2067 配额 → 复用 first_frame + 撞墙暂停）
```

### 模板填示例（美妆/护肤类，可参照改造）

```
【品牌】[品牌名]（字体偏好）
【品类】护肤单品（一瓶精华 / 一支口红）
【目标平台】小红书
【目标受众】22-35 女性，一二线城市，关注成分与肤感
【总时长】30 秒 = 5 段 × 6 秒
【核心视觉语言】极简大理石台面 + 晨光 + 玻璃质感 + 慢速液体倾倒
【节奏曲线】极简 → 入画 → 倾倒特写 → 质地流挂 → 品牌落版
【运镜】从俯拍固定 → 慢推近景 → 微距 tilt-up，全程柔光 + 浅景深
【声音】无旁白 + ambient piano + 滴答水声 → t=24s 柔和 drop
【字幕】3 张 title card + 成分关键词短句
【规格】9:16 竖屏 1080×1920
【预算】$2 USD
【约束】避免手部特写写实 / 模特涂脸 / 多产品横评
【首选视频模型】minimax Hailuo-2.3-Fast（I2V 不动 → 加光晕 + 微距 tilt-up）
```

---

## 六、复刻流程（拿到 brief 模板后的最短路径）

```
1. cp templates/brief-reverse-template.md → 你自己的 brief 草稿
2. 把方括号占位符替换成具体品牌/品类/场景
3. 在 OpenMontage 跑 cinematic pipeline：
     proposal (guided) → 选概念 → script (sample) → 看 5s sample → scene_plan → assets → edit → compose
4. 撞配额/超时 → agent 会 AskUserQuestion，按第四节决策
5. 渲染残片或完整成片落在 projects/<id>/renders/
```

---

## 七、相关引用

- `projects/xiaohongshu-neon-night-30s/` — 本模板反向拆解的源头项目
- `docs/ARCHITECTURE.md` — cinematic pipeline 整体架构
- `docs/single-port-arch.md` — 端口路由（8900/8901/8902）
- MEMORY.md `minimax-hailuo-2-3-quota-2067` — Hailuo 配额上限预警
- MEMORY.md `minimax-multimodal-newapi-verification-2026-09-09` — I2V wire-up 现状
- MEMORY.md `t2v-i2v-multi-image-reference-glossary-2026-09-10` — T2V/I2V/multi-image 能力档差异
