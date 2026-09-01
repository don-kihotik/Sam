from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path


class VideoProcessingError(RuntimeError):
    pass


def frame_sampling_fps(duration_seconds: int | None, max_frames: int) -> float:
    """Choose a rate that spreads at most max_frames across the full clip."""
    duration = max(float(duration_seconds or 30), 1.0)
    return min(2.0, max_frames / duration)


async def extract_video_frames(
    video: bytes,
    *,
    duration_seconds: int | None,
    max_frames: int = 12,
) -> list[bytes]:
    """Extract evenly spaced JPEG frames with ffmpeg."""
    if not video:
        raise VideoProcessingError("Video is empty")

    fps = frame_sampling_fps(duration_seconds, max_frames)
    with tempfile.TemporaryDirectory(prefix="sam-video-") as directory:
        root = Path(directory)
        source = root / "source.mp4"
        source.write_bytes(video)
        output_pattern = root / "frame-%03d.jpg"
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-vf",
            f"fps={fps:.6f},scale=1280:1280:force_original_aspect_ratio=decrease",
            "-frames:v",
            str(max_frames),
            "-q:v",
            "4",
            str(output_pattern),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise VideoProcessingError("Frame extraction timed out") from exc

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise VideoProcessingError(detail or "ffmpeg failed to extract frames")

        frames = [path.read_bytes() for path in sorted(root.glob("frame-*.jpg"))]
        if not frames:
            raise VideoProcessingError("No frames were extracted")
        return frames
