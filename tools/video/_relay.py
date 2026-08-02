"""Shared relay (中转站) protocol implementation for OpenMontage video tools.

Talks to a new-api compatible relay endpoint using its OpenAI-compatible video
API. new-api (and most aggregators like one-api) already bundle Kling and
Seedance(豆包) upstream channels, so OpenMontage only needs a thin submit →
poll → download client:

    POST {base}/v1/video/generations                    -> {"task_id", "status"}
    GET  {base}/v1/video/generations/{task_id}          -> {"task_id", "status", "url", "format", "metadata", "error"}

Statuses observed while polling: "queued", "processing", "succeeded", "failed".
A successful task carries a "url" pointing at the generated mp4.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tools.video._shared import probe_output


class RelayError(Exception):
    """Raised when the relay endpoint fails (network, API, timeout, or task failure)."""


def _submit(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    operation: str,
    image_url: str | None,
    duration: float | None,
    metadata: dict[str, Any] | None,
    timeout: float = 30.0,
) -> str:
    import requests

    payload: dict[str, Any] = {"model": model, "prompt": prompt}
    if operation == "image_to_video":
        if not image_url:
            raise RelayError(
                "image_to_video via relay requires an image_url (public URL); "
                "local paths are not uploaded by the relay path."
            )
        payload["image"] = image_url
    if duration is not None:
        payload["duration"] = float(duration)
    if metadata:
        payload["metadata"] = metadata

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    url = f"{base_url.rstrip('/')}/v1/video/generations"
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise RelayError(f"relay submit request failed: {exc}") from exc

    if resp.status_code >= 400:
        raise RelayError(
            f"relay submit rejected ({resp.status_code}): {resp.text[:500]}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise RelayError(f"relay submit returned non-JSON: {resp.text[:500]}") from exc

    task_id = data.get("task_id")
    if not task_id:
        raise RelayError(f"relay submit response missing task_id: {data}")
    return str(task_id)


def _poll(
    *,
    base_url: str,
    api_key: str,
    task_id: str,
    poll_interval: float,
    poll_timeout: float,
) -> dict[str, Any]:
    import requests

    headers = {"Authorization": f"Bearer {api_key}"}
    url = f"{base_url.rstrip('/')}/v1/video/generations/{task_id}"
    deadline = time.time() + poll_timeout

    while time.time() < deadline:
        try:
            resp = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as exc:
            raise RelayError(f"relay poll request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise RelayError(
                f"relay poll rejected ({resp.status_code}): {resp.text[:500]}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise RelayError(f"relay poll returned non-JSON: {resp.text[:500]}") from exc

        status = str(data.get("status", "")).lower()
        if status == "succeeded":
            return data
        if status == "failed":
            err = data.get("error") or {}
            message = err.get("message") if isinstance(err, dict) else err
            raise RelayError(f"relay video task failed: {message or data}")
        # queued / processing / anything unknown -> keep polling

        time.sleep(poll_interval)

    raise RelayError(f"relay video task {task_id} timed out after {poll_timeout}s")


def _download(video_url: str, output_path: Path, timeout: float = 180.0) -> None:
    import requests

    try:
        resp = requests.get(video_url, timeout=timeout)
    except requests.RequestException as exc:
        raise RelayError(f"relay video download failed: {exc}") from exc
    if resp.status_code >= 400:
        raise RelayError(
            f"relay video download rejected ({resp.status_code}): {resp.text[:500]}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(resp.content)


def generate_via_relay(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    operation: str = "text_to_video",
    image_url: str | None = None,
    duration: float | None = None,
    metadata: dict[str, Any] | None = None,
    output_path: str | Path,
    poll_interval: float = 5.0,
    poll_timeout: float = 900.0,
) -> dict[str, Any]:
    """Generate a video through a new-api compatible relay endpoint.

    Returns a metadata dict suitable for ToolResult.data:
      {
        "gateway": "new-api",
        "task_id": str,
        "model": model,
        "remote_url": str,
        "output": str,
        "output_path": str,
        "format": str,
        **probe_output(output_path),
      }

    Raises RelayError on any failure.
    """
    if not base_url:
        raise RelayError("VIDEO_RELAY_BASE_URL is not set.")
    if not api_key:
        raise RelayError("VIDEO_RELAY_API_KEY is not set.")
    if not prompt or not str(prompt).strip():
        raise RelayError("prompt is required.")

    task_id = _submit(
        base_url=base_url,
        api_key=api_key,
        model=model,
        prompt=str(prompt).strip(),
        operation=operation,
        image_url=image_url,
        duration=duration,
        metadata=metadata,
    )
    data = _poll(
        base_url=base_url,
        api_key=api_key,
        task_id=task_id,
        poll_interval=poll_interval,
        poll_timeout=poll_timeout,
    )

    video_url = data.get("url")
    if not video_url:
        raise RelayError(f"relay task succeeded but response has no url: {data}")

    out_path = Path(output_path)
    _download(str(video_url), out_path)

    fmt = data.get("format") or "mp4"
    return {
        "gateway": "new-api",
        "task_id": task_id,
        "model": model,
        "remote_url": str(video_url),
        "output": str(out_path),
        "output_path": str(out_path),
        "format": str(fmt),
        **probe_output(out_path),
    }
