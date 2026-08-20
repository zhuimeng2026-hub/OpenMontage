from pathlib import Path
from types import SimpleNamespace

from tools.audio.audio_mixer import AudioMixer


def test_replace_video_audio_builds_safe_ffmpeg_command(monkeypatch, tmp_path):
    video = tmp_path / "input.mp4"
    audio = tmp_path / "voice.mp3"
    output = tmp_path / "output.mp4"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    commands = []

    def fake_run(self, command, **kwargs):
        commands.append(command)
        output.write_bytes(b"muxed")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(AudioMixer, "run_command", fake_run)
    result = AudioMixer().execute({
        "operation": "replace_video_audio", "video_path": str(video),
        "audio_path": str(audio), "output_path": str(output),
    })

    assert result.success
    assert result.data["output"] == str(output)
    assert commands[0][0:2] == ["ffmpeg", "-y"]
    assert ["-map", "0:v:0"] == commands[0][6:8]
    assert "-shortest" in commands[0]
