# sample-pic Aug 19 生成批次记录

**日期**：2026-08-19
**作者**：agent
**状态**：仅记录，未提交（gitignore 排除）

## 概述

调用 `sample-pic/generate_bags.py`（MiniMax 图像生成）跑出一个 8 变体 × 8 视角 = 64 张图的批次，
加上 `projects/smart-suitcase-video` 已有的 4 张行李箱 hero/side/front/airport 视角图，
共 **84 张**新生成的示范图片。

## 落点

所有产物均落在 `projects/sample-pic/<variant>/` 下，由 `.gitignore` 的 `projects/` 规则自动排除。
与已 tracked 的 `sample-pic/generated/` 互为镜像——`generate_bags.py` 同时写两边。

| 变体 | 路径 | 张数 |
|---|---|---|
| 01-heritage-leather-briefcase | `projects/sample-pic/01-heritage-leather-briefcase/` | 8 |
| 03-designer-crossbody | `projects/sample-pic/03-designer-crossbody/` | 8 |
| 04-heritage-steamer-trunk | `projects/sample-pic/04-heritage-steamer-trunk/` | 8 |
| 06-evening-clutch | `projects/sample-pic/06-evening-clutch/` | 8 |
| 08-iconic-flap-handbag | `projects/sample-pic/08-iconic-flap-handbag/` | 8 |
| 09-aluminum-carry-on | `projects/sample-pic/09-aluminum-carry-on/` | 8 |
| 10-streetwear-belt-bag | `projects/sample-pic/10-streetwear-belt-bag/` | 8 |
| smart-suitcase-video | `projects/smart-suitcase-video/assets/suitcase-{hero,front,side,airport}.png` | 4 |

视角命名：`01_hero`、`02_product`、`03_detail`、`04_lifestyle`、`05_interior`、`06_material`、
`07_packaging`、`08_closing`。

## 为何不提交

`.gitignore` 显式排除 `projects/`：

```
# Project workspaces (generated assets, renders — all regenerable)
projects/
```

工作区契约：项目工作区只承载运行时产物（可被 `sample-pic/generate_bags.py` 完全重生）。
若要长期保存某次跑批的成果，应复制到 `sample-pic/generated/<variant>/`（tracked 镜像区）
而不是 `projects/`。

## 重新生成

```bash
# 全部变体
python sample-pic/generate_bags.py

# 单个变体
python sample-pic/generate_bags.py --variant 03-designer-crossbody
```

脚本读取 `sample-pic/ecommerce-product-image-prompts.md` 里的 prompt 模板，
调用 `tools/graphics/minimax_image.py`（MiniMax T2I），同时落 `projects/sample-pic/<variant>/`
和 `sample-pic/generated/<variant>/`。

## 关联

- `sample-pic/generate_bags.py` — 跑批脚本（已 commit，feat(graphics) cb93a81）
- `tools/graphics/minimax_image.py` — MiniMax 工具封装（已 commit，feat(graphics) cb93a81）
- `.agents/skills/minimax/SKILL.md` — Layer 3 vendor 知识（已 commit，feat(graphics) cb93a81）
- `sample-pic/ecommerce-product-image-prompts.md` — prompt 模板（已 tracked）
- `sample-pic/generated/` — 已 tracked 的 curated 镜像区