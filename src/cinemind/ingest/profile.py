"""Device profiles: what a TV (through Plex) plays directly, what can be fixed, what can't.

A profile is a JSON file. ``samsung-q60b`` is bundled; ``load_profile`` also accepts a path to
your own. Keys are camelCase in the file (the format MediaPipeline v1 used) and snake_case here.
Codec, profile and container names are ffprobe's and are matched without regard to case.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class Containers(_Model):
    direct: list[str]  # played as they are
    remux: list[str]  # rewrapped into the output container, streams copied
    reject: list[str]  # never accepted


class VideoCodecRule(_Model):
    profiles: list[str]
    max_bit_depth: int
    max_width: int
    max_height: int
    max_fps: float
    # A lower frame-rate limit above 1920x1088. The alias is explicit: the camel-case generator
    # would make it "max4KFps" (str.title() capitalises after a digit) and miss the key.
    max4k_fps: float | None = Field(None, alias="max4kFps")


class DolbyVision(_Model):
    reject_profiles: list[int]
    reject_when_no_compatible_base_layer: bool = True


class Video(_Model):
    direct: dict[str, VideoCodecRule]
    review: list[str] = []  # decoded by the TV, but worth confirming once
    reject_reasons: dict[str, str] = {}
    dolby_vision: DolbyVision


class AudioTarget(_Model):
    codec: str
    bitrate: str
    channels: int


class Audio(_Model):
    direct: list[str]
    aac_profiles: list[str] = []
    convert: list[str] = []
    prefer_existing_compatible_track_same_language: bool = True
    target_multichannel: AudioTarget
    target_stereo: AudioTarget


class Subtitles(_Model):
    direct: list[str]
    convert_to_srt: list[str] = []
    drop: list[str] = []


class Bitrate(_Model):
    warn_above_mbps: float


class Output(_Model):
    container: str = "matroska"
    extension: str = ".mkv"


class DeviceProfile(_Model):
    profile_name: str
    device: str  # short name used in messages, e.g. "Q60B"
    brand: str  # e.g. "Samsung", for the Dolby Vision messages
    containers: Containers
    video: Video
    audio: Audio
    subtitles: Subtitles
    bitrate: Bitrate
    output: Output = Output()


BUNDLED = {"samsung-q60b"}


def load_profile(name_or_path: str = "samsung-q60b") -> DeviceProfile:
    """A bundled profile by name, or a profile JSON file by path."""
    if name_or_path in BUNDLED:
        text = (resources.files("cinemind.ingest") / "profiles" / f"{name_or_path}.json").read_text(
            encoding="utf-8"
        )
    else:
        text = Path(name_or_path).read_text(encoding="utf-8")
    return DeviceProfile.model_validate(json.loads(text))
