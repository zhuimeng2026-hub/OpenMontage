# BFF MVP — curl 客户端 Cookbook

> 配套:`docs/bff_mvp_api_reference_2026-08-31.md`(API 详解)
> 全部例子默认 `BFF=http://127.0.0.1:18907`,开发期设 `WEIXIN_MOCK_AUTH=1` 后任意 `MOCK_*` code 都能登录。
> 每个例子都是单条命令,直接复制粘贴能跑。

## 0. 环境变量(开一次就够)

```bash
export BFF="http://127.0.0.1:18907"
# 任选一个随机串当本次会话的 JWT(开发模式省去登录)
export JWT=$(curl -s -X POST "$BFF/api/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"code":"MOCK_demo_'$$'"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
export IUID=$(curl -s "$BFF/api/me/jwt" -H "Authorization: Bearer $JWT" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['internal_user_id'])")

# 建一个租户
export TID=$(curl -s -X POST "$BFF/api/tenants" \
  -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"Demo Co"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo "JWT=${JWT:0:20}...  IUID=$IUID  TID=$TID"
```

之后所有调用都用 `Authorization: Bearer $JWT` + `X-Tenant-Id: $TID`。

---

## 1. 用户 / 租户

```bash
# 登录
curl -s -X POST "$BFF/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"code":"MOCK_demo"}'

# 读自己
curl -s "$BFF/api/me/jwt" -H "Authorization: Bearer $JWT"

# 列我的租户
curl -s "$BFF/api/tenants" -H "Authorization: Bearer $JWT"

# 加成员(Bob 加入 Alice 的 TID)
curl -s -X POST "$BFF/api/tenants/$TID/members" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  -H 'Content-Type: application/json' \
  -d "{\"user_id\":\"$IUID\"}"
```

---

## 2. 商品 + 素材 + Manifest

```bash
# 建商品
export PID=$(curl -s -X POST "$BFF/api/products" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Demo Bag","category":"fashion","sku":"demo-001"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "PID=$PID"

# 读商品
curl -s "$BFF/api/products/$PID" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"

# 上传素材(multipart,role 必填)
curl -s -X POST "$BFF/api/products/$PID/assets" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  -F "role=primary" -F "file=@/opt/OpenMontage_Voicebox/remotion-composer/public/_staged/the-refactor-serenade-sample/hook_terminal_desk.png"

# 列素材
curl -s "$BFF/api/products/$PID/assets" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"

# 读 AI 自动分类的 manifest(等几秒等 AI 处理)
curl -s "$BFF/api/products/$PID/manifest" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"

# 人工修正某个 asset 的 role(把 asset_id 替换成上面返回里的)
curl -s -X PUT "$BFF/api/products/$PID/manifest/<asset_id>" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  -H 'Content-Type: application/json' \
  -d '{"role":"detail","caption":"主图修正"}'
```

---

## 3. 项目

```bash
# 建项目
export VPID=$(curl -s -X POST "$BFF/api/video-projects" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  -H 'Content-Type: application/json' \
  -d "{\"product_id\":\"$PID\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "VPID=$VPID"

# 读项目
curl -s "$BFF/api/video-projects/$VPID" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"

# 改 creative brief
curl -s -X PUT "$BFF/api/video-projects/$VPID/brief" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  -H 'Content-Type: application/json' \
  -d '{"creative_brief":{"goal":"promo","tone":"energetic"},"reference_mode":"balanced"}'

# 绑定参考视频(需要先签发文件 key,见 §5)
curl -s -X POST "$BFF/api/video-projects/$VPID/reference" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  -H 'Content-Type: application/json' \
  -d '{"file_key":"<signed_file_key>"}'

# 读状态(轻量轮询用)
curl -s "$BFF/api/video-projects/$VPID/status" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"

# 取消(任何非终态都行)
curl -s -X POST "$BFF/api/video-projects/$VPID/cancel" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"
```

---

## 4. 阶段流水线(核心)— 每个阶段立即返回 `jb_xxx`,后台跑 MCP

```bash
# 触发 storyboard → 立即 STORYBOARD_READY,job 后台落 artifacts
curl -s -X POST "$BFF/api/video-projects/$VPID/storyboard" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"
# → {"job_id":"jb_xxx","status":"STORYBOARD_READY","async":true,...}

# 触发 animatic → 立即 ANIMATIC_RENDERING
curl -s -X POST "$BFF/api/video-projects/$VPID/animatic" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"
# → {"job_id":"jb_xxx","status":"ANIMATIC_RENDERING",...}

# 触发 sample → 立即 SAMPLE_RENDERING
curl -s -X POST "$BFF/api/video-projects/$VPID/sample" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"

# 触发 render → 立即 FINAL_RENDERING(自动 Reserve 50 credits)
curl -s -X POST "$BFF/api/video-projects/$VPID/render" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"
# quota 不足 → 402 {"error":"insufficient credits for render","required":50}

# 轮询 job 进度 + 取 artifact
export JB=$(curl -s "$BFF/api/jobs/<job_id>" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
curl -s "$BFF/api/jobs/$JB" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"
# status: running → succeeded;external_run_id + artifacts_json 都会有

# 提取 artifact 里的 preview_url(animatic / render 形状)
python3 -c "
import json, requests
job = requests.get('$BFF/api/jobs/$JB',
  headers={'Authorization':'Bearer $JWT','X-Tenant-Id':'$TID'}).json()
art = json.loads(job.get('artifacts_json','{}'))
print('preview_url:', art.get('preview_url'))
print('files:', art.get('files'))
print('scenes:', [s.get('preview_url') for s in art.get('scenes',[])])
"
```

---

## 5. 用户 approve(Phase 7)— sample → render 的强制关

```bash
# 等 sample 阶段完成,GET /status 看到 SAMPLE_READY 后:
curl -s -X POST "$BFF/api/video-projects/$VPID/approve" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"
# → 200 {"status":"WAITING_APPROVAL","approved_by":"iu_...","approved_at":"..."}
# → 409 {"error":"illegal transition from CREATED","allowed_from":["SAMPLE_READY","WAITING_APPROVAL"]}

# 再次 approve(幂等):仍 200,status 保持 WAITING_APPROVAL,approved_at 刷新

# 读 approve 后状态(approved_by / approved_at 已写库)
curl -s "$BFF/api/video-projects/$VPID" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"
```

---

## 6. Agent Gateway(8 动词)— Phase 5

```bash
# 通用 POST 动词(GET /production-status 改 query string)
for verb in analyze-product-assets analyze-reference-video \
            generate-storyboard generate-animatic generate-sample \
            render-final cancel-production; do
  echo "=== $verb ==="
  curl -s -X POST "$BFF/api/gateway/$verb" \
    -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
    -H 'Content-Type: application/json' \
    -d "{\"project_id\":\"$VPID\",\"payload\":{}}"
  echo
done

# GET 形态的 production-status
curl -s "$BFF/api/gateway/production-status?project_id=$VPID" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"

# 垃圾 verb(确认 404)
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  "$BFF/api/gateway/garbage-verb" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"
# → 404
```

---

## 7. OM 状态聚合(13 档)— Phase 5

```bash
# 已知 raw → 映射到 13 档
for raw in mcp-raw COMPLETED CREATED ANIMATIC_RENDERING FINAL_RENDERING \
           pending running "error_unknown" "" totally_unknown_xyz; do
  echo -n "raw='$raw' → "
  curl -s "$BFF/api/status/lookup?raw=$raw" \
    -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['unified'])"
done

# 列出所有支持的状态(可探测覆盖)
curl -s "$BFF/api/status/lookup?raw=anything" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  | python3 -c "import sys,json; print('\n'.join(json.load(sys.stdin)['supported_raw_states']))"
```

---

## 8. 配额

```bash
# 读 quota
curl -s "$BFF/api/quota" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"
# → {"available_credits":100,"reserved_credits":0,"consumed_credits":0,"tier":"free"}

# 手动 reserve / consume / refund(直接调 quota 端点,一般 render 流程自动做)
curl -s -X POST "$BFF/api/quota/reserve" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  -H 'Content-Type: application/json' \
  -d '{"amount":50,"job_id":"manual-test"}'
# → {"reservation_id":"ql_xxx"} ;402 if insufficient

curl -s -X POST "$BFF/api/quota/consume" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  -H 'Content-Type: application/json' \
  -d '{"reservation_id":"<reservation_id>"}'

curl -s -X POST "$BFF/api/quota/refund" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  -H 'Content-Type: application/json' \
  -d '{"reservation_id":"<reservation_id>"}'
```

---

## 9. 文件(签发 + 拉)

```bash
# 签发一个文件 key(假设 key 已经在 file_acl 表里)
curl -s "$BFF/api/files/sign?key=<file_key>&op=read" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"

# 通过签名 URL 直接拉(无 Bearer 也能用,因为签名就是授权)
curl -s -o /tmp/asset.png "$BFF/api/files/<file_key>"
```

---

## 10. 错误码速查

```bash
# 401 — 无 JWT
curl -s -o /dev/null -w "%{http_code}\n" "$BFF/api/me/jwt"

# 403 — 跨 tenant
# 用 Bob 的 JWT + Alice 的 X-Tenant-Id 调 Alice 的资源 → 403
curl -s -o /dev/null -w "%{http_code}\n" \
  "$BFF/api/video-projects/$VPID" \
  -H "Authorization: Bearer $BOB_JWT" -H "X-Tenant-Id: <ALICE_TID>"

# 404 — 不存在
curl -s -o /dev/null -w "%{http_code}\n" \
  "$BFF/api/video-projects/vp_does_not_exist" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"

# 409 — 非法状态机转移
curl -s -X POST "$BFF/api/video-projects/$VPID/storyboard" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  | python3 -m json.tool
# 注意要在已完成的项目上重复触发 — 已 STORYBOARD_READY 再触发 storyboard → 409

# 402 — quota 不足(把 quota 扣完再 render)
curl -s -X POST "$BFF/api/video-projects/$VPID/render" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID" \
  -w "\nstatus=%{http_code}\n"

# 503 — MCP_BASE_URL 没配(启动 BFF 时不设 MCP_BASE_URL 然后调 stage 端点)
MCP_BASE_URL="" /tmp/frameflow-bff-mvp-p7 &
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  "$BFF/api/video-projects/$VPID/storyboard" \
  -H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID"
# → 503
```

---

## 11. 完整 E2E 链路(粘贴即跑)

```bash
#!/usr/bin/env bash
# 一键走完:login → product → project → 4 阶段 → approve → render → 取最终 mp4 URL
set -eu
BFF="${BFF:-http://127.0.0.1:18907}"
JQ='python3 -c "import sys,json; print(json.load(sys.stdin)$1)"'

# login + tenant
JWT=$(curl -s -X POST "$BFF/api/auth/login" -H 'Content-Type: application/json' \
  -d '{"code":"MOCK_e2e_'$$'"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
TID=$(curl -s -X POST "$BFF/api/tenants" -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' -d '{"name":"E2E"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
H=(-H "Authorization: Bearer $JWT" -H "X-Tenant-Id: $TID")

# product
PID=$(curl -s -X POST "$BFF/api/products" "${H[@]}" -H 'Content-Type: application/json' \
  -d '{"name":"E2E"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# project
VPID=$(curl -s -X POST "$BFF/api/video-projects" "${H[@]}" -H 'Content-Type: application/json' \
  -d "{\"product_id\":\"$PID\"}" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# helper: trigger stage + poll job to succeeded
trigger () {
  local stage="$1" want="$2"
  local jb=$(curl -s -X POST "$BFF/api/video-projects/$VPID/$stage" "${H[@]}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
  for i in $(seq 1 60); do
    sleep 2
    s=$(curl -s "$BFF/api/jobs/$jb" "${H[@]}" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
    [ "$s" = "succeeded" ] && break
    [ "$s" = "failed" ] && { echo "FAIL: stage=$stage status=$s"; return 1; }
  done
  echo "  [stage=$stage] jb=$jb status=$s"
}

trigger storyboard STORYBOARD_READY
trigger animatic   ANIMATIC_READY
trigger sample     SAMPLE_READY

# approve
curl -s -X POST "$BFF/api/video-projects/$VPID/approve" "${H[@]}"
echo

# render (stub --succeed-render 才走得到 COMPLETED)
trigger render      COMPLETED

# 取最终 mp4 URL
curl -s "$BFF/api/jobs/$(curl -s "$BFF/api/video-projects/$VPID" "${H[@]}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))")" "${H[@]}"
# 实际生产:把最后这段换成"查 production_jobs 取最新 render 的 jb_id 再 GET /jobs/:jb"
echo "E2E OK"
```

---

## 12. 一行 curl 健康检查(给监控用)

```bash
curl -sf http://127.0.0.1:18907/healthz && echo "BFF alive"
```

---

## 13. Stub MCP 自测(开发期,无 MCP_BASE_URL 也能跑通 gate)

```bash
# 起本地 stub + BFF
nohup /opt/OpenMontage_Voicebox/scripts/mvp_dev/phase_7/mcp_stub_server.py \
  18910 --succeed-render > /tmp/stub.log 2>&1 &
MCP_BASE_URL=http://127.0.0.1:18910/mcp MCP_API_TOKEN=t \
  WEIXIN_MOCK_AUTH=1 /tmp/frameflow-bff-mvp-p7 &

# 等 /healthz 通
for i in 1 2 3 4 5 6 7 8 9 10; do
  curl -sf http://127.0.0.1:18907/healthz && break || sleep 0.5
done

# 跑全套 gate
bash /opt/OpenMontage_Voicebox/scripts/mvp_dev/phase_7/gate.sh
```