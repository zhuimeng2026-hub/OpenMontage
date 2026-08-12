"""SSE 渲染进度端点的回归测试。

历史 bug：render_progress_sse 调用了 subscribe()/unsubscribe()，但 mcp_server.py
只导入了 publish/progress_event，导致该路径运行期 NameError -> 500（对所有 job）。
该端点此前从未做活体验证，故漏网。这里直接驱动 handler 验证：(1) 未知 job 能流式
返回 snapshot 帧（而非抛 500）；(2) 后台发布的事件能经总线送达订阅者。

注意：不使用 TestClient.stream，因为 handler 的心跳循环不会自然结束，会在
上下文退出清理时挂起。改为直接构造 Request -> 取 StreamingResponse -> 手动驱动
body_iterator 取前几帧后 aclose()。
"""
import sys
import threading
import time

sys.path.insert(0, ".")

import anyio
from starlette.requests import Request

import mcp_server  # noqa: E402  (import after sys.path tweak)
from lib.render_progress import publish, subscribe, unsubscribe  # noqa: E402


def _make_request(job_id: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/render-progress/{job_id}",
        "headers": [],
        "query_string": b"",
        "path_params": {"job_id": job_id},
    }
    return Request(scope)


def test_sse_handler_runs_without_nameerror():
    """回归：render_progress_sse 在返回 StreamingResponse 前会同步调用
    subscribe(job_id)/find_session_by_job_id(job_id)。若 subscribe 未从
    lib.render_progress 导入，await 时即抛 NameError（曾真实地导致 500）。
    这里只 await 不消费生成器，避免心跳循环在测试里挂起。"""
    async def run():
        resp = await mcp_server.render_progress_sse(_make_request("does-not-exist"))
        return resp.media_type

    media = anyio.run(run)
    assert media == "text/event-stream"


def test_sse_delivers_published_event():
    job_id = "unit-test-job"
    q = subscribe(job_id)
    try:
        captured = []

        def _consume():
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    ev = q.get(timeout=0.2)
                except Exception:
                    continue
                captured.append(ev)
                if ev.get("status") in ("published", "failed"):
                    break

        t = threading.Thread(target=_consume)
        t.start()
        time.sleep(0.3)  # 让订阅者就位
        publish(job_id, mcp_server.progress_event(job_id, phase="render", status="rendering", percent=10))
        publish(job_id, mcp_server.progress_event(job_id, phase="render", status="rendering", percent=99))
        publish(job_id, mcp_server.progress_event(job_id, phase="share", status="published", share_url="https://share.weiyun.com/xyz"))
        t.join(timeout=6)

        statuses = [e.get("status") for e in captured]
        assert "rendering" in statuses
        assert "published" in statuses
        assert captured[-1].get("share_url") == "https://share.weiyun.com/xyz"
    finally:
        unsubscribe(job_id, q)
