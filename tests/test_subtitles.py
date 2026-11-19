"""Subtitles: decoding, SRT round trips, release matching, and the speech-sync check."""

from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest

from cinemind.ingest.probe import probe
from cinemind.subtitles import release
from cinemind.subtitles.srt import Cue, decode, format_srt, parse
from cinemind.subtitles.sync import (
    FRAME,
    AudioWindows,
    Window,
    audio_windows,
    check_sync,
    frame_energy,
)

HEBREW = "שלום עולם"


# -- text ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [b"\xef\xbb\xbf" + HEBREW.encode("utf-8"), b"\xff\xfe" + HEBREW.encode("utf-16-le"),
     b"\xfe\xff" + HEBREW.encode("utf-16-be"), HEBREW.encode("utf-8"),
     HEBREW.encode("cp1255")],  # classic Hebrew subtitles: no BOM, not UTF-8
)  # fmt: skip
def test_every_common_encoding_decodes(data):
    assert decode(data) == HEBREW


SRT = """1
00:00:01,500 --> 00:00:03,000
First line
second line

2
00:01:02.25 --> 00:01:04,000 X1:0
Second cue
"""


def test_parse_and_retime():
    cues = parse(SRT)
    assert cues == [Cue(1.5, 3.0, "First line\nsecond line"), Cue(62.25, 64.0, "Second cue")]
    shifted = parse(format_srt(cues, scale=1.0, offset=-2.0))
    assert shifted[0] == Cue(0.0, 1.0, "First line\nsecond line")  # start clamped at 0
    assert parse(format_srt(cues, offset=-3.5)) == [Cue(58.75, 60.5, "Second cue")]  # 1st dropped
    stretched = parse(format_srt(cues, scale=25 / 23.976))
    assert stretched[1].start == pytest.approx(62.25 * 25 / 23.976, abs=0.001)
    assert format_srt([]) == ""


# -- release names -------------------------------------------------------------------------------


def test_release_traits_and_the_v1_group_fix():
    t = release.traits("Show.S01E01.1080p.AMZN.WEB-DL.DDP5.1.H.264-GROUP")
    assert (t.group, t.source, t.resolution, t.services) == ("group", "webdl", "1080p", ["amzn"])
    # v1 cut ".H.264-GROUP" off as if it were a file extension and read the group as "dl".
    assert release.traits("Show.S01E01.1080p.WEB-DL.x264-GROUP.mkv").group == "group"


def test_release_scores_rank_the_same_release_first():
    video = "Movie.2010.1080p.BluRay.x264-SPARKS.mkv"
    same = release.score(video, "Movie.2010.1080p.BluRay.x264-SPARKS")
    sibling = release.score(video, "Movie.2010.720p.BRRip.x264-OTHER")
    web = release.score(video, "Movie.2010.1080p.WEBRip.x264-OTHER")
    cam = release.score(video, "Movie.2010.HDCAM-NOBODY")
    extended = release.score(video, "Movie.2010.EXTENDED.1080p.BluRay.x264-SPARKS")
    assert same == 85 and same > sibling > web > cam
    assert extended == same - 15  # another cut: rarely in sync
    assert cam < 0


# -- the sync check (synthetic speech) ------------------------------------------------------------

DURATION, WINDOW, PAD = 3000.0, 300.0, 15.0
STARTS = [round(DURATION * f - WINDOW / 2) for f in (0.15, 0.32, 0.5, 0.68, 0.85)]


def _speech(seed: int) -> list[tuple[float, float]]:
    """Random dialogue: 1.5-5 s of speech, then 0.5-4 s of silence, for the whole film."""
    rng = np.random.default_rng(seed)
    t, out = 0.0, []
    while t < DURATION:
        length = rng.uniform(1.5, 5.0)
        out.append((t, min(DURATION, t + length)))
        t += length + rng.uniform(0.5, 4.0)
    return out


def _audio(speech: list[tuple[float, float]], seed: int = 1) -> AudioWindows:
    """Energy windows as audio_windows would decode them: loud in speech, quiet between."""
    rng = np.random.default_rng(seed)
    windows = []
    for start in STARTS:
        times = (start - PAD) + np.arange(int((WINDOW + 2 * PAD) / FRAME)) * FRAME
        loud = np.zeros(times.size, dtype=bool)
        for s, e in speech:
            loud |= (times >= s) & (times < e)
        energy = np.where(loud, 1.0, -1.0) + rng.normal(0, 0.8, times.size)
        windows.append(Window(float(start), (energy - energy.mean()) / energy.std()))
    return AudioWindows(DURATION, WINDOW, PAD, windows)


def _cues(speech, scale=1.0, offset=0.0) -> list[Cue]:
    return [Cue(s * scale + offset, e * scale + offset, "…") for s, e in speech]


SPEECH = _speech(7)
AUDIO = _audio(SPEECH)


def test_a_matching_subtitle_is_synced():
    result = check_sync(_cues(SPEECH), AUDIO)
    assert result.status == "synced" and result.offset == 0.0 and result.confidence > 8


def test_a_late_subtitle_is_shifted_back():
    result = check_sync(_cues(SPEECH, offset=7.0), AUDIO)
    assert result.status == "shifted" and result.offset == pytest.approx(-7.0, abs=0.15)


def test_a_25fps_subtitle_is_rescaled():
    pal = _cues(SPEECH, scale=23.976 / 25)  # timed for the sped-up PAL release
    result = check_sync(pal, AUDIO)
    assert result.status == "rescaled" and result.scale == pytest.approx(25 / 23.976)
    assert abs(result.offset) <= 0.3


def test_another_films_subtitle_does_not_match():
    result = check_sync(_cues(_speech(99)), AUDIO)
    assert result.status in ("nomatch", "inconclusive") and result.confidence < 5


def test_too_little_to_judge_is_inconclusive():
    assert check_sync(_cues(SPEECH[:10]), AUDIO).status == "inconclusive"
    assert check_sync(_cues(SPEECH), None).status == "inconclusive"
    long = [Cue(DURATION + 500.0, DURATION + 501.0, "x")] * 30
    assert check_sync(long, AUDIO).status == "nomatch"  # runs past the end at every speed


def test_frame_energy_is_z_normalised():
    loud = (np.ones(800) * 8000).astype("<i2").tobytes()
    quiet = (np.ones(800) * 10).astype("<i2").tobytes()
    energy = frame_energy(loud + quiet + loud)
    assert energy.size == 3 and energy[0] > 0 > energy[1]
    assert abs(energy.mean()) < 1e-9 and frame_energy(b"").size == 0


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                    reason="needs FFmpeg on PATH")  # fmt: skip
async def test_audio_windows_are_decoded_with_ffmpeg(tmp_path):
    clip = tmp_path / "clip.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "aevalsrc=0.4*sin(2*PI*700*t)*lt(mod(t\\,7)\\,3):s=48000:d=240",
                    "-c:a", "flac", str(clip)], check=True)  # fmt: skip
    info = await probe(clip, shutil.which("ffprobe"))
    audio = await audio_windows(str(clip), info, shutil.which("ffmpeg"))
    assert audio is not None and audio.window_sec == 60 and not audio.centre_only
    assert [w.start for w in audio.windows] == [18.0, 90.0, 162.0]
    assert all(w.energy.size == int((60 + 2 * 15) / FRAME) for w in audio.windows)
