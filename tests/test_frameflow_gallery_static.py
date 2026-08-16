from pathlib import Path


INDEX_HTML = Path(__file__).parents[1] / "frameflow" / "bff" / "web" / "index.html"


def _section(html: str, start: str, end: str) -> str:
    return html.split(start, 1)[1].split(end, 1)[0]


def test_gallery_uses_real_queue_data_without_static_demo_cards():
    html = INDEX_HTML.read_text(encoding="utf-8")
    gallery = _section(html, '<section class="view" id="view-gallery">', "<!-- QUEUE -->")

    assert 'id="gallery-body"' in gallery
    assert "function loadGallery" in html
    assert "function renderGallery" in html
    assert "/api/render-queue" in html
    assert "safeDownloadURL(j.share_url)" in html
    assert "credentials: 'include'" in html
    assert "paintLoadError" in html
    assert 'class="vcard video-link"' in html
    assert "role=\"button\" tabindex=\"0\"" in html
    assert "aria-label=\"打开视频：" in html
    assert "openGalleryCard" in html
    assert "window.open(url, '_blank', 'noopener,noreferrer')" in html
    assert "打开微云 / 播放下载" in html
    assert "e.target.closest('.qdownload')" in html
    assert "生成微云链接" in html
    assert "尝试恢复微云链接" in html
    assert "function republishRender" in html
    assert "'/api/render-queue/' + encodeURIComponent(jobID) + '/republish'" in html
    assert "e.target.closest('.qrepublish')" in html
    assert "r.headers.get('content-type')" in html
    assert "application\\/json" in html
    assert "服务端恢复接口未部署或未返回有效链接" in html
    assert "safeDownloadURL(data.share_url)" in html
    assert "JOB-DEMO" not in html
    assert "javascript:" not in gallery

    for demo_title in ("团队年度合集", "节日祝福卡片", "产品功能演示"):
        assert demo_title not in gallery


def test_dashboard_recent_videos_are_dynamic():
    html = INDEX_HTML.read_text(encoding="utf-8")
    dashboard = _section(html, '<section class="view active" id="view-dashboard">', "<!-- CREATE -->")

    assert 'id="dash-recent-videos"' in dashboard
    assert "function renderRecent" in html
    assert "夏日旅行混剪" not in dashboard
    assert "产品发布会开场" not in dashboard
    assert "dash-recent-videos" in html
