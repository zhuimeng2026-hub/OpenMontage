"""Regression: custom-composition (用户自定义脚本) 必须渲染真实内容，不能红屏。

Bug: Babel 自动 JSX runtime 编译出 `require("react/jsx-runtime")`，但
CustomComposition.tsx 只特殊处理 `"remotion"`，webpack 又无法解析 new Function
里的动态 require，导致编译失败、整个视频被错误边界染成红色。
Fix: 在 CustomComposition.tsx 中静态引入 `react/jsx-runtime` 并通过 userRequire 暴露。
"""
import subprocess
import tempfile
from pathlib import Path

import pytest

SAMPLE_TSX = r'''
import {AbsoluteFill, useCurrentFrame, staticFile} from "remotion";
export const MyComposition = ({images, durationPerImage = 3, fps = 30, width = 1080, height = 1920}) => {
  const frame = useCurrentFrame();
  const idx = Math.min(images.length - 1, Math.floor((frame / fps) / durationPerImage));
  const src = images[idx];
  return (
    <AbsoluteFill style={{backgroundColor: "#111", justifyContent: "center", alignItems: "center"}}>
      {src ? <img src={staticFile(src)} style={{width: "100%", height: "100%", objectFit: "cover"}} /> : null}
    </AbsoluteFill>
  );
};
'''


def _sh(*args):
    subprocess.run(args, check=True, capture_output=True)


def test_custom_composition_renders_real_content():
    out = Path(tempfile.mkdtemp(prefix="custom_compose_qa_"))
    img_a = out / "a.png"
    img_b = out / "b.png"
    _sh(
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x2266cc:s=640x360:d=1",
        "-frames:v", "1", str(img_a),
    )
    _sh(
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0xcc6622:s=640x360:d=1",
        "-frames:v", "1", str(img_b),
    )

    from tools.video.video_compose import VideoCompose

    vc = VideoCompose()
    out_mp4 = out / "custom.mp4"
    res = vc._remotion_render({
        "edit_decisions": {
            "version": "1.0",
            "cuts": [],
            "render_runtime": "remotion",
            "renderer_family": "custom-composition",
            "composition_mode": "custom",
            "custom_code": SAMPLE_TSX,
            "images": [str(img_a), str(img_b)],
            "duration_per_image": 2,
            "metadata": {
                "title": "qa-custom",
                "script_id": "custom",
                "targetDurationSeconds": 4,
                "compose_target": {"width": 1080, "height": 1920, "fit": "cover"},
            },
        },
        "output_path": str(out_mp4),
        "profile": "tiktok",
        "staging_id": "qa_custom_composition",
    })
    assert res.success, f"render failed: {res.error}"
    assert out_mp4.exists(), "output mp4 missing"

    # Extract first frame and assert it is NOT the red error screen (#7F1D1D).
    frame_png = out / "frame1.png"
    _sh(
        "ffmpeg", "-y", "-i", str(out_mp4), "-frames:v", "1", "-q:v", "2",
        str(frame_png),
    )
    from PIL import Image

    im = Image.open(frame_png).convert("RGB")
    colors = im.getcolors(maxcolors=1000000)
    colors.sort(reverse=True)
    top_count, (r, g, b) = colors[0]
    total = im.size[0] * im.size[1]
    # ErrorScreen is ~#7F1D1D (r 127, g 29, b 29). If the whole frame is red, fail.
    is_red_error = (
        top_count == total
        and 90 < r < 160
        and g < 60
        and b < 60
    )
    assert not is_red_error, (
        "CustomComposition rendered the red error screen; "
        f"dominant rgb=({r},{g},{b}). Likely compile/runtime error in user code."
    )
