from pathlib import Path
import io
import sys

from tools.publishers.weiyun_upload import WeiyunUpload
from tools.publishers import weiyun_upload_lib


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


def test_upload_file_is_safe_on_cp936_stdout(monkeypatch, tmp_path):
    """Upload progress must not crash when Windows stdout is GBK/cp936."""
    video = tmp_path / "render.mp4"
    video.write_bytes(b"mp4")

    class Cp936Stdout(io.StringIO):
        encoding = "cp936"

        def write(self, value):
            value.encode(self.encoding)
            return super().write(value)

    responses = iter([
        {"channel_list": [{"id": 1, "offset": 0, "len": 3}], "upload_key": "uk", "ex": ""},
        {"upload_state": 2, "file_id": "file-1", "filename": video.name},
    ])
    monkeypatch.setattr(weiyun_upload_lib, "mcp_call", lambda *args, **kwargs: next(responses))
    stdout = Cp936Stdout()
    monkeypatch.setattr(sys, "stdout", stdout)

    result = weiyun_upload_lib.upload_file(
        str(video), "https://example.test/mcp", {"WyHeader": "mcp_token=test"}
    )

    assert result == {"file_id": "file-1", "filename": video.name}
    assert "upload completed" in stdout.getvalue()
