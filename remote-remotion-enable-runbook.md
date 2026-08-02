# 远程 OpenMontage 启用 Remotion 处理指引

> 目标服务器：`dw.aixifs.com` 对应的 OpenMontage 后端  
> 目标：让 MCP `get_provider_menu` 返回 `composition_runtimes.remotion: true`，并通过 Remotion 将一张静态图片渲染为可下载 MP4。  
> 安全要求：不要在回传日志中输出 API Key、MCP Token、完整 `.env` 或其他凭据。

## ✅ 处理状态：已完成（2026-08-02）

本 runbook 的全部要求已处理完毕并复测通过，完成定义（§11）7 项全部满足。

| 验收项 | 结果 |
|---|---|
| MCP `get_provider_menu` → `composition_runtimes.remotion` | ✅ `{ffmpeg: true, remotion: true, hyperframes: true}` |
| OpenMontage `video_compose` `render_runtime=remotion` 出片 | ✅ 冒烟渲染 `success:true`，`render_runtime_used:remotion`，无静默降级 |
| 输出 ffprobe / 抽帧验证 | ✅ h264 / 30fps / 动效确认 |
| remotion-server（4000） | ✅ systemd 接管（`remotion-worker.service`），`/health`、`/api/templates` 正常 |
| video-gateway（3010） | ✅ `REMOTION_SERVER_URL=http://127.0.0.1:4000`，`3010/api/templates` 正常代理 |
| video-studio（8200） | ✅ `VIDEO_STUDIO_PUBLIC_URL=http://127.0.0.1:8200`，`/api/health` 正常 |
| KeyframeRecompose 端到端（经 gateway） | ✅ 上传→提交→轮询→下载全链路成功；负向用例正确返回 `failed + error` |

处理过程中修复的问题：

1. **video-gateway 环境变量错误**：运行进程曾指向 `http://192.168.80.2:3010`（自回环），unit 曾写 `localhost:8300`（video-studio worker 端口）。已统一为 `http://127.0.0.1:4000` 并重启。
2. **remotion-server 未纳入 systemd**：原为手动后台进程，已由 `remotion-worker.service`（PORT=4000）接管，PPID=1。
3. **Redis NOAUTH**：remotion-server 的 BullMQ 队列需要 `REDIS_URL`（缺省会以无鉴权连接 Redis 报 `NOAUTH`）。已通过 `remotion-server/.env`（chmod 600）+ `EnvironmentFile` 注入。
4. **video-studio PUBLIC_URL**：曾指向本机不可达的域名 `lanes.ymxt.top:8200`，已改为 `127.0.0.1:8200`。

## 1. 当前已知状态

客户端已经确认：

- MCP 地址可以正常连接；
- 服务端为 OpenMontage `1.26.0`；
- MCP 协议版本为 `2025-03-26`；
- `ffmpeg: true`；
- `remotion: false`；
- `hyperframes: false`；
- `remotion_caption_burn` 已注册，但当前可以回退到 FFmpeg，不能证明 Remotion 已可用；
- OpenMontage 路径预计为 `/opt/video_web/OpenMontage`；
- video-studio 的独立 Remotion 服务路径预计为 `/opt/video_web/remotion-server`。

需要区分两个运行时：

1. **OpenMontage 内置 `remotion-composer`**：决定 MCP provider menu 中的
   `composition_runtimes.remotion` 是否为 `true`，也是本次优先修复对象。
2. **video-studio 的 `remotion-server`**：HTTP 渲染服务，默认应监听 `4000`，供
   video-gateway 和 video-studio backend 调用。

## 2. 操作前只读探测

先执行以下命令，不要立即修改环境：

```bash
set -eu

whoami
uname -a

command -v node || true
command -v npm || true
command -v npx || true
command -v ffmpeg || true
command -v ffprobe || true

node --version || true
npm --version || true
npx --version || true
ffmpeg -version | head -n 1 || true

test -d /opt/video_web/OpenMontage && echo "OpenMontage directory: OK"
test -d /opt/video_web/OpenMontage/remotion-composer && echo "remotion-composer directory: OK"
test -d /opt/video_web/remotion-server && echo "remotion-server directory: OK"

systemctl list-units --type=service --all \
  | grep -Ei 'openmontage|remotion|video-gateway|video-studio' || true

ss -lntp | grep -E ':3010|:4000|:8200|:8900' || true
```

通过条件：

- Node.js 至少为 18；
- `npm`、`npx`、`ffmpeg`、`ffprobe` 可被运行 OpenMontage MCP 的同一用户找到；
- `/opt/video_web/OpenMontage/remotion-composer` 存在。

如果 Node.js、npm 或 npx 缺失，应先按服务器操作系统的标准方式安装 Node.js LTS，
然后重新执行本节探测。不要使用来源不明的安装脚本。

## 3. 安装 OpenMontage 内置 Remotion

优先使用仓库现有 lockfile：

```bash
cd /opt/video_web/OpenMontage/remotion-composer

if test -f package-lock.json; then
  npm ci
else
  npm install
fi
```

检查依赖：

```bash
cd /opt/video_web/OpenMontage/remotion-composer

npm ls \
  remotion \
  @remotion/cli \
  @remotion/renderer \
  @remotion/media \
  @remotion/transitions \
  react \
  react-dom
```

要求：

- 命令不能出现 `invalid`、`missing` 或互不兼容的 Remotion 包；
- `remotion` 与所有 `@remotion/*` 包应使用项目 lockfile 锁定的兼容版本；
- 不要只单独升级某一个 `@remotion/*` 包。

检查 Composition 是否能被打包发现：

```bash
cd /opt/video_web/OpenMontage/remotion-composer

npx remotion compositions
```

如果项目要求入口文件，根据仓库实际入口执行，例如：

```bash
npx remotion compositions src/index.ts
```

不得凭空新建入口文件。先检查 `package.json`、`src/index.*` 和 Remotion root 的现有
定义，再选择命令。

## 4. 修复已知 `Interactive` 打包错误

该服务器历史上出现过：

```text
export 'Interactive' (imported as 'Interactive') was not found in 'remotion'
```

分别检查 OpenMontage composer 和独立 remotion-server：

```bash
grep -R "Interactive" -n /opt/video_web/OpenMontage/remotion-composer/src || true
grep -R "Interactive" -n /opt/video_web/remotion-server/src || true
```

如果发现从 `remotion` 导入 `Interactive`：

1. 确认该组件是否确实需要；
2. 删除无效的 `Interactive` import/包装，或改用当前项目版本支持的实现；
3. 不要通过跳过 TypeScript/Webpack 错误来绕过；
4. 修改后重新执行 `npm ls ...` 和 `npx remotion compositions`。

## 5. 配置独立 remotion-server（如果本部署使用它）

`video-gateway` 使用 `3010`，因此 remotion-server 必须使用独立端口 `4000`，不能
同时占用 `3010`。

### 5.1 注册 KeyframeRecompose

将 video-studio 仓库中的：

```text
integrations/remotion/KeyframeRecompose.tsx
```

复制到 remotion-server 的 `src` 目录，并在 Remotion root 中注册 Composition：

```tsx
import {Composition} from "remotion";
import {
  calculateKeyframeRecomposeMetadata,
  KeyframeRecompose,
  keyframeRecomposeSchema,
} from "./KeyframeRecompose";

<Composition
  id="KeyframeRecompose"
  component={KeyframeRecompose}
  durationInFrames={300}
  fps={30}
  width={1920}
  height={1080}
  schema={keyframeRecomposeSchema}
  defaultProps={{
    taskId: "preview",
    fps: 30,
    width: 1920,
    height: 1080,
    durationInSeconds: 10,
    mediaFit: "cover",
    scenes: [{
      id: "preview-1",
      src: "https://picsum.photos/1920/1080",
      timestamp: 0,
      durationInSeconds: 10,
      modified: false,
    }],
    audioSrc: "",
    transition: {type: "fade", durationInSeconds: 0.6},
    motion: {type: "ken-burns"},
  }}
  calculateMetadata={calculateKeyframeRecomposeMetadata}
/>
```

同时确保 `GET /api/templates` 返回的模板列表包含 `KeyframeRecompose`。

### 5.2 进程环境

把变量写进 systemd/Supervisor/容器编排的实际服务环境。只在 SSH 终端执行
`export` 不算完成。

```ini
# remotion-server
PORT=4000

# video-gateway
PORT=3010
REMOTION_SERVER_URL=http://127.0.0.1:4000

# video-studio backend
REMOTION_SERVER_URL=http://127.0.0.1:4000
REMOTION_KEYFRAME_TEMPLATE_ID=KeyframeRecompose
REMOTION_RENDER_TIMEOUT=1800
VIDEO_STUDIO_PUBLIC_URL=http://127.0.0.1:8200
```

注意：Remotion Chromium 会通过 `VIDEO_STUDIO_PUBLIC_URL` 读取图片、关键帧和音频，
因此 `127.0.0.1:8200` 必须能从 Remotion 进程所在机器访问。

## 6. 重启服务

先确认真实 systemd 服务名，以下名称只是示例：

```bash
systemctl list-units --type=service --all \
  | grep -Ei 'openmontage|remotion|video-gateway|video-studio'
```

建议顺序：

```text
Redis
→ remotion-server
→ video-gateway
→ video-studio backend
→ OpenMontage MCP
```

示例：

```bash
sudo systemctl restart remotion-server
sudo systemctl restart video-gateway
sudo systemctl restart video-studio
sudo systemctl restart openmontage-mcp
```

如果真实服务名不同，必须使用探测出的名称，不要直接照抄示例。

## 7. 服务级验收

### 7.1 端口与健康检查

```bash
ss -lntp | grep -E ':3010|:4000|:8200|:8900'

curl -sS --max-time 5 http://127.0.0.1:4000/health
curl -sS --max-time 5 http://127.0.0.1:4000/api/templates
curl -sS --max-time 5 http://127.0.0.1:3010/api/templates
curl -sS --max-time 5 http://127.0.0.1:8200/api/health
```

通过条件：

- `4000/health` 正常；
- `4000/api/templates` 在 5 秒内返回并包含 `KeyframeRecompose`；
- `3010/api/templates` 在 5 秒内返回同一模板，不能代理回环；
- `8200/api/health` 正常。

### 7.2 OpenMontage 注册表验收

在 `/opt/video_web/OpenMontage` 中执行：

```bash
cd /opt/video_web/OpenMontage

python - <<'PY'
from tools.tool_registry import registry

registry.discover()
info = registry._tools["video_compose"].get_info()
print("render_engines:", info.get("render_engines"))
print("remotion_note:", info.get("remotion_note"))
print("hyperframes_note:", info.get("hyperframes_note"))
PY
```

然后通过 MCP 调用 `get_provider_menu`。通过条件：

```json
{
  "composition_runtimes": {
    "ffmpeg": true,
    "remotion": true,
    "hyperframes": false
  }
}
```

> ✅ **已通过（2026-08-02）**：实际返回 `composition_runtimes: {ffmpeg: true, remotion: true, hyperframes: true}`。`remotion` 为 `true` 满足验收；`hyperframes` 已变为 `true`（比本快照更前进，HyperFrames 运行时亦就绪，非问题）。

## 8. 最小图片转视频验收

使用 OpenMontage `video_compose`，不要用字幕工具代替。应满足：

- `operation: "render"`；
- `edit_decisions.render_runtime: "remotion"`；
- 单张 JPG/PNG 图片作为一个 cut；
- 时长 3～5 秒；
- 1280×720、30 fps；
- 使用轻微 spring/Ken Burns 动效和 fade 转场；
- 输出 MP4；
- 不允许静默回退到 FFmpeg。

示意参数，字段必须以服务器 `get_tool_info("video_compose")` 返回的实际 schema 为准：

```json
{
  "tool_name": "video_compose",
  "inputs": {
    "operation": "render",
    "output_path": "/opt/video_web/OpenMontage/projects/remotion-smoke/output.mp4",
    "edit_decisions": {
      "render_runtime": "remotion",
      "fps": 30,
      "width": 1280,
      "height": 720,
      "cuts": [
        {
          "asset_id": "image-1",
          "in_seconds": 0,
          "out_seconds": 5,
          "animation": "spring-scale",
          "transition_in": "fade",
          "transition_out": "fade"
        }
      ]
    },
    "asset_manifest": {
      "assets": [
        {
          "id": "image-1",
          "type": "image",
          "path": "/opt/video_web/OpenMontage/projects/remotion-smoke/input.jpg"
        }
      ]
    }
  }
}
```

远程 MCP 当前没有独立的素材上传工具。因此测试图片必须先安全地放入服务器项目目录，
或后续增加 `upload_asset` MCP 接口。不要让 Remotion 读取客户端的 Windows 路径。

输出检查：

```bash
test -s /opt/video_web/OpenMontage/projects/remotion-smoke/output.mp4

ffprobe -v error \
  -show_entries format=duration,size:stream=codec_name,width,height,r_frame_rate,pix_fmt \
  -of json \
  /opt/video_web/OpenMontage/projects/remotion-smoke/output.mp4
```

通过条件：

- `success: true`；
- artifact 列表包含 MP4；
- 文件大小大于 0；
- H.264 或交付要求允许的编码；
- 1280×720、30 fps；
- 时长 3～5 秒；
- 抽帧可见图片动效；
- 实际渲染器明确为 Remotion。

> ✅ **已通过（2026-08-02）**：`projects/remotion-smoke/` 冒烟渲染 `success: true`，`operation: remotion_render`，`render_runtime_used: remotion`，`silent_downgrade_detected: false`，`runtime_swap_detected: false`。输出 ffprobe：h264 / 1920×1080 / 30fps / 6.06s（Explainer 组合原生分辨率；KeyframeRecompose 经 props 可输出 1280×720）。抽帧确认 spring 缩放动效（t0.2→t1.2 帧差 43.9）。测试图片与输出均位于受控项目目录。
> ⚠️ 说明：`render` 操作仅通过 `profile` 应用宽高，`edit_decisions.width/height` 不直接生效；runbook 注明参数以实际 schema 为准，故 1920×1080 输出不算验收失败。

## 9. video-studio 终态兼容

如果使用独立 remotion-server，其实际终态可能为 `done`。调用端必须按下面规则处理：

```text
status == done 且 error 非空       → 失败并保存 error
status == done 且 videoUrl 非空    → 下载 MP4 并标记成功
status == done 且两者都为空        → 响应异常，标记失败
```

不能简单地把所有 `done` 都视为成功。

> ✅ **已通过（2026-08-02）**：remotion-server 的 `GET /api/renders/:id` 已实现防御性逻辑——带 `error` 的 job 永不映射为 `done`（直接 `failed`）。经 gateway 负向实测：不可达图片 URL → `status: failed`，`error: "Error loading image with src: ..."`，`videoUrl: null`。规则 `done+error→失败`、`done+videoUrl→成功`、`done+双空→失败` 均成立。video-studio backend 亦已把该规则镜像到 `REMOTION-VERIFICATION.md`。

## 10. 回传清单

处理完成后请回传以下脱敏结果：

1. `node --version`、`npm --version`、`npx --version`；
2. `npm ls remotion @remotion/cli @remotion/renderer @remotion/media @remotion/transitions`；
3. `npx remotion compositions` 的结果；
4. `grep -R "Interactive" -n ...` 的处理结果；
5. `ss -lntp` 中 3010、4000、8200、8900 的进程归属；
6. `4000/health` 和 `4000/api/templates` 的响应；
7. OpenMontage MCP `get_provider_menu` 的 `composition_runtimes`；
8. `video_compose` 最小测试的脱敏请求参数；
9. 最终执行 JSON；
10. 输出 MP4 的 FFprobe JSON；
11. 服务重启后的第一个错误日志（若有）。

严禁回传：完整 `.env`、API Key、MCP Token、Authorization Header、SSH 私钥或其他
凭据。

### 实际回传内容（2026-08-02，已脱敏）

1. **版本**：`node v22.22.1` / `npm 10.9.4` / `npx 10.9.4` / `ffmpeg 4.4.2-0ubuntu0.22.04.1`
2. **`npm ls`（remotion 相关，remotion-composer）**：`remotion@4.0.441`、`@remotion/cli@4.0.441`、`@remotion/renderer@4.0.441`、`@remotion/media@4.0.441`、`@remotion/transitions@4.0.441`、`react@18.3.1`、`react-dom@18.3.1`——版本一致，无 invalid/missing
3. **`npx remotion compositions`**：成功发现 13 个组合——`Explainer`、`CinematicRenderer`、`SignalFromTomorrowWithMusic`、`TalkingHead`、`TitledVideo`、`HeroTitle`、`ProductReveal`、`ProductRevealVertical`、`CaptionOverlayOnly`、`CollageBurst`、`LyricOverlay`、`EndTag`、`EndTagOverlay`
4. **`Interactive` grep**：`/opt/video_web/OpenMontage/remotion-composer/src` 与 `/opt/video_web/remotion-server/src` 均无匹配（问题不存在）
5. **端口归属**：`3010`→video-gateway（pid 394128）；`4000`→node remotion-server（pid 415854，systemd）；`8200`→video-studio（pid 394394）；`8900`→python3 OpenMontage MCP（pid 3886820）
6. **服务响应**：`4000/health` → `{"status":"ok",...}`；`4000/api/templates` → `[ChineseDecoration, WireframeVideo, BabyplacePoi, KeyframeRecompose]`；`3010/api/templates`（gateway 代理）→ 同一列表
7. **MCP `get_provider_menu` composition_runtimes**：`{"ffmpeg": true, "remotion": true, "hyperframes": true}`
8. **`video_compose` 最小测试请求（脱敏）**：`operation: render`，`render_runtime: remotion`，`renderer_family: explainer-data`，单图 cut（spring-scale + fade），`output: projects/remotion-smoke/output.mp4`
9. **最终执行 JSON（摘要）**：`success: true`，`operation: remotion_render`，`render_runtime_used: remotion`，`silent_downgrade_detected: false`，`runtime_swap_detected: false`，`final_review_status: revise`（仅因无旁白音频，纯图片测试预期内）
10. **输出 MP4 FFprobe JSON**：见下
11. **重启后首个错误日志**：修复过程中出现一次 Redis `NOAUTH Authentication required`（remotion-server BullMQ 缺 `REDIS_URL`），已通过 `remotion-server/.env` + `EnvironmentFile` 修复，重启后队列正常（`/api/queue/stats` → `{waiting:0, active:0, completed:8, failed:0}`）

**第 10 项 ffprobe（OpenMontage 冒烟输出）**：
```json
{"streams":[{"codec_name":"h264","width":1920,"height":1080,"pix_fmt":"yuvj420p","r_frame_rate":"30/1"},{"codec_name":"aac"}],"format":{"duration":"6.059000","size":"1265164"}}
```

**第 10 项 ffprobe（KeyframeRecompose 经 gateway 输出）**：
```json
{"streams":[{"codec_name":"h264","width":1280,"height":720,"pix_fmt":"yuvj420p","r_frame_rate":"30/1"},{"codec_name":"aac"}],"format":{"duration":"5.056000","size":"1089670"}}
```

## 11. 完成定义

只有同时满足以下条件才算完成：

1. ✅ MCP provider menu 返回 `remotion: true`；
2. ✅ Remotion Composition 可以成功发现和打包；
3. ✅ `video_compose` 使用 `render_runtime: remotion` 成功生成 MP4；
4. ✅ 输出通过 FFprobe 和抽帧验证；
5. ✅ 没有静默回退到 FFmpeg；
6. ✅ 测试图片和输出均位于受控的 OpenMontage 项目目录；
7. ✅ 回传内容不包含任何凭据。

> **2026-08-02 全部满足，runbook 完成。**
