/**
 * 帧流 FrameFlow — Remotion MCP 调用骨架（浏览器侧，统一走 BFF）
 * =================================================================
 * 安全约定：浏览器【绝不直接】持有 MCP_API_TOKEN。所有 MCP 调用统一经由
 * 你自建的 BFF / 网关转发，由 BFF 持有 token 并维护 MCP 会话（mcp_session_id）。
 *
 * BFF 需实现的契约（与 OpenMontage 的 MCP 工具一一对应）：
 *   1) POST {bffBaseUrl}/api/mcp
 *        请求体：{ "tool": "<mcp 工具名>", "args": { ... } }
 *        响应体：该工具 extract 之后的结构化结果
 *                （与 om_mcp_probe.py 的 extract() 一致，即 result.content[].text 的 JSON）
 *   2) GET  {bffBaseUrl}/api/render-progress/{jobId}
 *        返回 text/event-stream（SSE）。BFF 需 proxy_buffering off 透传。
 *
 * 重要：BFF 必须维护「同一用户 → 同一 MCP 会话」的亲和性，否则 upload_asset_chunk
 * 上传的图片与后续 create_remotion_video_share 不在同一会话，会找不到素材。
 *
 * 演示模式：当 config.js 中 remotion.bffBaseUrl 为空时，本文件自动进入
 * 「演示骨架」——本地模拟分块上传与渲染进度，不产生任何真实网络请求，
 * 仅用于评审前端交互。配置好 BFF 后即自动切换到真实调用。
 */
(function () {
  'use strict';

  var cfg = (window.FF_CONFIG && window.FF_CONFIG.remotion) || {};
  var BFF = (cfg.bffBaseUrl || '').replace(/\/+$/, '');
  var DEMO = !BFF;
  var CHUNK = 400 * 1000; // 单片二进制字节数，与 om_mcp_probe.py 的 chunk=400_000 对齐

  // ---- base64 / hex 工具 ----
  function b64FromArrayBuffer(buf) {
    var bytes = new Uint8Array(buf);
    var binary = '';
    var step = 0x8000; // 32KB 步长，避免 btoa 超长字符串爆栈
    for (var i = 0; i < bytes.length; i += step) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
    }
    return btoa(binary);
  }
  function bufToHex(buf) {
    var bytes = new Uint8Array(buf);
    var s = '';
    for (var i = 0; i < bytes.length; i++) s += bytes[i].toString(16).padStart(2, '0');
    return s;
  }
  async function sha256Hex(buf) {
    var digest = await crypto.subtle.digest('SHA-256', buf);
    return bufToHex(digest);
  }

  // ---- BFF 调用封装 ----
  async function mcpCall(tool, args) {
    if (DEMO) return { __demo: true, tool: tool, args: args || {} };
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 30000); // 30s 超时，防止请求挂死
    try {
      var resp = await fetch(BFF + '/api/mcp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include', // 让 BFF 通过 cookie 维持用户会话
        body: JSON.stringify({ tool: tool, args: args || {} }),
        signal: ctrl.signal
      });
      if (!resp.ok) throw new Error('BFF.' + tool + ' HTTP ' + resp.status);
      return await resp.json();
    } finally {
      clearTimeout(timer);
    }
  }

  // ---- 分块上传单张图片：start -> append* -> complete ----
  // 协议严格对齐 OpenMontage：按二进制字节切片后各自 base64，offset 用二进制偏移。
  async function chunkUpload(file, opts) {
    opts = opts || {};
    var projectId = opts.projectId || 'frameflow-default';
    var onProgress = opts.onProgress || function () {};
    var buf = await file.arrayBuffer();
    var n = buf.byteLength;
    var mime = file.type ||
      (/\.png$/i.test(file.name) ? 'image/png' : 'image/jpeg');
    var sha = await sha256Hex(buf);
    var safe = file.name.replace(/[^\w.\-]/g, '_');

    if (DEMO) {
      var sent = 0;
      while (sent < n) {
        await new Promise(function (r) { setTimeout(r, 60); });
        sent = Math.min(n, sent + CHUNK);
        onProgress(Math.round(sent / n * 100));
      }
      return {
        success: true, demo: true, upload_id: 'demo-' + Date.now(),
        asset_count: 1, status: 'collecting_assets',
        message: '已收到 1 张图片（演示）。可继续上传，完成后点击「开始渲染」。'
      };
    }

    // 1) start
    var start = await mcpCall('upload_asset_chunk', {
      operation: 'start', project_id: projectId, filename: safe,
      total_bytes: n, mime_type: mime, sha256: sha
    });
    var uploadId = (start && start.upload_id) || (start && start.data && start.data.upload_id);
    if (!uploadId) throw new Error('chunk start 失败：' + JSON.stringify(start));

    // 2) append 若干片（按二进制字节切片，offset 用二进制偏移）
    var offset = 0;
    while (offset < n) {
      var piece = buf.slice(offset, offset + CHUNK);
      var cb64 = b64FromArrayBuffer(piece);
      var ap = await mcpCall('upload_asset_chunk', {
        operation: 'append', project_id: projectId, filename: safe,
        upload_id: uploadId, offset: offset, chunk_base64: cb64
      });
      if (!ap || !ap.success) {
        throw new Error('chunk append@' + offset + ' 失败：' + JSON.stringify(ap));
      }
      offset += piece.byteLength;
      onProgress(Math.round(offset / n * 100));
    }

    // 3) complete
    return await mcpCall('upload_asset_chunk', {
      operation: 'complete', project_id: projectId, filename: safe, upload_id: uploadId
    });
  }

  // ---- 创建视频（非阻塞）：返回 render_job_id + batch_id ----
  async function createVideo(opts) {
    opts = opts || {};
    var args = {
      project_id: opts.projectId || 'frameflow-default',
      duration_per_image: opts.durationPerImage || 3.0,
      aspect_ratio: opts.aspectRatio || '9:16',
      title: opts.title || '帧流作品'
    };
    if (DEMO) {
      return {
        success: true, demo: true,
        render_job_id: 'JOB-' + Math.random().toString(16).slice(2, 8).toUpperCase(),
        batch_id: 'BATCH-' + Date.now(),
        message: '已提交渲染任务（演示）'
      };
    }
    return await mcpCall('create_remotion_video_share', args);
  }

  // ---- 轮询渲染状态（SSE 不可用时的兜底）----
  async function getRenderStatus(jobId) {
    if (DEMO) return null;
    return await mcpCall('get_render_status', { render_job_id: jobId });
  }

  // ---- SSE 实时进度（优先）----
  function subscribeProgress(jobId, onEvent, onError) {
    if (DEMO) {
      var pct = 0;
      var timer = setInterval(function () {
        pct += 3 + Math.random() * 5;
        if (pct >= 100) {
          pct = 100; clearInterval(timer);
          onEvent({ status: 'published', percent: 100,
            share_url: 'https://share.example.com/demo-' + jobId });
        } else {
          onEvent({ status: 'rendering', percent: Math.round(pct) });
        }
      }, 400);
      return function () { clearInterval(timer); };
    }
    var es = new EventSource(BFF + '/api/render-progress/' + encodeURIComponent(jobId));
    es.onmessage = function (e) {
      try { onEvent(JSON.parse(e.data)); } catch (err) { onEvent({ raw: e.data }); }
    };
    es.onerror = function (err) { if (onError) onError(err); es.close(); };
    return function () { es.close(); };
  }

  window.FFMCP = {
    demo: DEMO,
    bffBaseUrl: BFF,
    chunkSize: CHUNK,
    mcpCall: mcpCall,
    chunkUpload: chunkUpload,
    createVideo: createVideo,
    getRenderStatus: getRenderStatus,
    subscribeProgress: subscribeProgress
  };
})();
