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
  var demoMode = DEMO; // 运行时可切换：点「体验演示模式」后置 true，使全流程走本地模拟
  var CHUNK = 400 * 1000; // 单片二进制字节数，与 om_mcp_probe.py 的 chunk=400_000 对齐
  var MCP_CALL_TIMEOUT_MS = 120000; // LAN/远程 MCP 上传分块允许更长的网络与处理时间

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
  // HTTP deployments on a LAN IP are not secure contexts, so Web Crypto is
  // unavailable there. Keep the upload protocol working without weakening the
  // SHA-256 checksum contract expected by the BFF.
  function sha256HexFallback(buf) {
    var K = [
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b,
      0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01,
      0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7,
      0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
      0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152,
      0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
      0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
      0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
      0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819,
      0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116, 0x1e376c08,
      0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f,
      0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
      0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
    ];
    var H = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    var bytes = new Uint8Array(buf);
    var bitLen = bytes.length * 8;
    var total = (((bytes.length + 9 + 63) >> 6) << 6);
    var data = new Uint8Array(total);
    data.set(bytes);
    data[bytes.length] = 0x80;
    var view = new DataView(data.buffer);
    view.setUint32(total - 4, bitLen >>> 0);
    view.setUint32(total - 8, Math.floor(bitLen / 0x100000000));
    function rotr(x, n) { return (x >>> n) | (x << (32 - n)); }
    for (var offset = 0; offset < total; offset += 64) {
      var W = new Uint32Array(64);
      for (var i = 0; i < 16; i++) W[i] = view.getUint32(offset + i * 4);
      for (var j = 16; j < 64; j++) {
        var s0 = rotr(W[j - 15], 7) ^ rotr(W[j - 15], 18) ^ (W[j - 15] >>> 3);
        var s1 = rotr(W[j - 2], 17) ^ rotr(W[j - 2], 19) ^ (W[j - 2] >>> 10);
        W[j] = (W[j - 16] + s0 + W[j - 7] + s1) >>> 0;
      }
      var a = H[0], b = H[1], c = H[2], d = H[3];
      var e = H[4], f = H[5], g = H[6], h = H[7];
      for (var k = 0; k < 64; k++) {
        var S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        var ch = (e & f) ^ (~e & g);
        var t1 = (h + S1 + ch + K[k] + W[k]) >>> 0;
        var S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        var maj = (a & b) ^ (a & c) ^ (b & c);
        var t2 = (S0 + maj) >>> 0;
        h = g; g = f; f = e; e = (d + t1) >>> 0;
        d = c; c = b; b = a; a = (t1 + t2) >>> 0;
      }
      H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0;
      H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
      H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0;
      H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
    }
    return H.map(function (x) { return x.toString(16).padStart(8, '0'); }).join('');
  }
  async function sha256Hex(buf) {
    if (window.crypto && window.crypto.subtle) {
      var digest = await window.crypto.subtle.digest('SHA-256', buf);
      return bufToHex(digest);
    }
    return sha256HexFallback(buf);
  }

  // ---- BFF 调用封装 ----
  async function mcpCall(tool, args) {
    if (demoMode) return { __demo: true, tool: tool, args: args || {} };
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, MCP_CALL_TIMEOUT_MS);
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

  // ---- 分块上传单个媒体文件：start -> append* -> complete ----
  // 协议严格对齐 OpenMontage：按二进制字节切片后各自 base64，offset 用二进制偏移。
  async function chunkUpload(file, opts) {
    opts = opts || {};
    var projectId = opts.projectId || 'frameflow-default';
    var onProgress = opts.onProgress || function () {};
    var buf = await file.arrayBuffer();
    var n = buf.byteLength;
    var mime = file.type ||
      (/\.png$/i.test(file.name) ? 'image/png' :
       /\.webp$/i.test(file.name) ? 'image/webp' :
       /\.(mp4|m4v)$/i.test(file.name) ? 'video/mp4' :
       /\.mov$/i.test(file.name) ? 'video/quicktime' :
       /\.webm$/i.test(file.name) ? 'video/webm' :
       /\.wav$/i.test(file.name) ? 'audio/wav' :
       /\.m4a$/i.test(file.name) ? 'audio/mp4' :
       /\.mp3$/i.test(file.name) ? 'audio/mpeg' : 'application/octet-stream');
    var sha = await sha256Hex(buf);
    // MCP 负责将不安全的 basename 自动改名；浏览器端不要先把中文/特殊字符
    // 替换成以下划线开头的名称，否则会在到达 MCP 前丢失原始名称并触发校验失败。
    var filename = (file && typeof file.name === 'string' ? file.name.trim() : '');
    if (!filename) {
      var extMatch = /\.([A-Za-z0-9]{1,10})$/.exec(file && file.name || '');
      var mimeExt = {
        'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp',
        'image/gif': 'gif', 'image/bmp': 'bmp', 'image/tiff': 'tiff'
      }[mime] || 'bin';
      filename = 'upload-' + sha.slice(0, 16) + '.' + (extMatch ? extMatch[1].toLowerCase() : mimeExt);
    }

    if (demoMode) {
      var sent = 0;
      while (sent < n) {
        await new Promise(function (r) { setTimeout(r, 60); });
        sent = Math.min(n, sent + CHUNK);
        onProgress(Math.round(sent / n * 100));
      }
      return {
        success: true, demo: true, upload_id: 'demo-' + Date.now(),
        asset: {id: 'demo-asset-' + Date.now(), filename: file.name, mime_type: mime},
        asset_count: 1, status: 'collecting_assets',
        message: '已收到 1 个媒体文件（演示）。'
      };
    }

    // 1) start
    var start = await mcpCall('upload_asset_chunk', {
      operation: 'start', project_id: projectId, filename: filename,
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
        operation: 'append', project_id: projectId, filename: filename,
        upload_id: uploadId, offset: offset, chunk_base64: cb64
      });
      if (!ap || !ap.success) {
        throw new Error('chunk append@' + offset + ' 失败：' + JSON.stringify(ap));
      }
      offset += piece.byteLength;
      onProgress(Math.round(offset / n * 100));
    }

    // 3) complete
    var complete = await mcpCall('upload_asset_chunk', {
      operation: 'complete', project_id: projectId, filename: filename, upload_id: uploadId
    });
    if (!complete || complete.success !== true || (complete.data && complete.data.success === false)) {
      throw new Error('chunk complete 失败：' + JSON.stringify(complete));
    }
    return complete;
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
    if (demoMode) {
      return {
        success: true, demo: true,
        render_job_id: 'JOB-' + Math.random().toString(16).slice(2, 8).toUpperCase(),
        batch_id: 'BATCH-' + Date.now(),
        message: '已提交渲染任务（演示）'
      };
    }
    var result = await mcpCall('create_remotion_video_share', args);
    var jobId = result && (result.render_job_id || (result.data && result.data.render_job_id));
    if (!jobId || result.success === false || (result.data && result.data.success === false)) {
      throw new Error('渲染提交失败：' + JSON.stringify(result));
    }
    return result;
  }

  async function createCaptionedVideo(opts) {
    opts = opts || {};
    if (demoMode) return {success:true, render_job_id:'MEDIA-' + Date.now(), status:'queued'};
    return await mcpCall('create_captioned_video_share', {
      project_id: opts.projectId,
      video_asset_id: opts.videoAssetId,
      language: opts.language || 'zh',
      subtitle_style: opts.subtitleStyle || 'short_video',
      title: opts.title || '帧流字幕视频'
    });
  }

  async function createClonedVoiceVideo(opts) {
    opts = opts || {};
    if (demoMode) return {success:true, render_job_id:'VOICE-' + Date.now(), status:'queued'};
    return await mcpCall('create_cloned_voice_video_share', {
      project_id: opts.projectId,
      video_asset_id: opts.videoAssetId,
      voice_sample_asset_id: opts.voiceSampleAssetId,
      script: opts.script || '',
      audio_mode: 'replace',
      subtitle: opts.subtitle !== false,
      language: opts.language || 'zh',
      subtitle_style: opts.subtitleStyle || 'short_video',
      title: opts.title || '帧流克隆配音视频',
      voice_consent: opts.voiceConsent === true
    });
  }

  // ---- 轮询渲染状态（SSE 不可用时的兜底）----
  async function getRenderStatus(jobId) {
    if (demoMode) return null;
    return await mcpCall('get_render_status', { render_job_id: jobId });
  }

  // ---- SSE 实时进度（优先）----
  function subscribeProgress(jobId, onEvent, onError) {
    if (demoMode) {
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
    var pollTimer = null;
    var stopped = false;
    var pollAttempts = 0;
    function stop(){
      stopped = true;
      if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
      es.close();
    }
    function poll(){
      if (stopped || pollAttempts++ >= 300) {
        if (!stopped && onError) onError(new Error('render status polling timed out'));
        return;
      }
      getRenderStatus(jobId).then(function(res){
        if (stopped) return;
        var data = (res && res.data) || res || {};
        var status = String(data.status || '').toLowerCase();
        onEvent(data);
        if (status === 'published' || status === 'done' || status === 'success' || status === 'completed' || status === 'finished' || status === 'failed' || status === 'error') {
          stop();
          return;
        }
        pollTimer = setTimeout(poll, 3000);
      }).catch(function(err){
        if (stopped) return;
        if (pollAttempts >= 300) { if (onError) onError(err); return; }
        pollTimer = setTimeout(poll, 5000);
      });
    }
    es.onmessage = function (e) {
      try { onEvent(JSON.parse(e.data)); } catch (err) { onEvent({ raw: e.data }); }
    };
    es.onerror = function () {
      // EventSource cannot be reliably resumed through every reverse proxy.
      // Switch to the MCP status API so a dropped SSE stream still reaches a
      // terminal state in the create page and queue.
      es.close();
      poll();
    };
    return stop;
  }

  window.FFMCP = {
    get demo() { return demoMode; },
    setDemo: function (on) { demoMode = (on === undefined) ? true : !!on; },
    bffBaseUrl: BFF,
    chunkSize: CHUNK,
    mcpCall: mcpCall,
    chunkUpload: chunkUpload,
    createVideo: createVideo,
    createCaptionedVideo: createCaptionedVideo,
    createClonedVoiceVideo: createClonedVoiceVideo,
    getRenderStatus: getRenderStatus,
    subscribeProgress: subscribeProgress
  };
})();
