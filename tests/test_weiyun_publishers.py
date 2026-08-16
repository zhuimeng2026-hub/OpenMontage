from pathlib import Path

from tools.publishers.weiyun_upload import WeiyunUpload


def test_weiyun_upload_rejects_empty_file_id(monkeypatch, tmp_path):
    video = tmp_path / "render.mp4"
    video.write_bytes(b"mp4")
    monkeypatch.setenv("WEIYUN_MCP_TOKEN", "test-token")
    monkeypatch.setattr(
        "tools.publishers.weiyun_upload.upload_file",
        lambda **kwargs: {"file_id": "", "filename": Path(kwargs["file_path"]).name},
    )

    result = WeiyunUpload().execute({"video_path": str(video)})

    assert result.success is False
    assert "without a file_id" in result.error
