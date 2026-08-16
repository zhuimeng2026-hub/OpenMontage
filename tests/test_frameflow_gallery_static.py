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
