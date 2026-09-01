import pytest

from app.video import frame_sampling_fps


@pytest.mark.parametrize(
    ("duration", "max_frames", "expected"),
    [
        (5, 12, 2.0),
        (30, 12, 0.4),
        (60, 12, 0.2),
        (None, 12, 0.4),
    ],
)
def test_frame_sampling_fps_spreads_frames_across_clip(duration, max_frames, expected):
    assert frame_sampling_fps(duration, max_frames) == pytest.approx(expected)
