"""Write the pack-matching eval cases: ``shows.json``, ``cases.jsonl`` and ``CASES.md``.

Each case is a torrent's video files, the episode(s) wanted, and the right answer: the file
that holds them, or none. The shows are fictional (so a model can't answer from memory and
the repo carries no real release names), but the file names follow real conventions: scene
releases, anime fansub batches numbered across seasons, title-only names, season folders,
double episodes, two-part finales, specials, and the decoys that sit next to them (samples,
creditless openings, previews, extras).

The output is deterministic. To change a case, change it here and rerun:

    uv run python evals/pack_matching/make_cases.py
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path

HERE = Path(__file__).parent
MB = 2**20


def _titles(seed: str, count: int) -> list[str]:
    rng = random.Random(seed)
    adjectives = [
        "Quiet",
        "Broken",
        "Silver",
        "Hidden",
        "Last",
        "Distant",
        "Burning",
        "Paper",
        "Hollow",
        "Restless",
        "Golden",
        "Narrow",
        "Open",
        "Bitter",
        "Bright",
        "Salt",
        "Midnight",
        "Second",
        "Crooked",
        "Gentle",
        "Iron",
        "Wild",
        "Empty",
        "Lucky",
    ]
    nouns = [
        "Harbor",
        "Letter",
        "Bridge",
        "Signal",
        "Promise",
        "Window",
        "Current",
        "Lantern",
        "Debt",
        "Crossing",
        "Orchard",
        "Engine",
        "Garden",
        "Storm",
        "Door",
        "Map",
        "Tide",
        "Shift",
        "Recipe",
        "Ledger",
        "Station",
        "Parcel",
        "Compass",
        "Feast",
        "Thaw",
    ]
    out: list[str] = []
    while len(out) < count:
        title = f"The {rng.choice(adjectives)} {rng.choice(nouns)}"
        if title not in out:
            out.append(title)
    return out


def _season(slug: str, season: int, count: int, fixed: dict[int, str] | None = None) -> list[dict]:
    titles = _titles(f"{slug}-{season}", count)
    for episode, title in (fixed or {}).items():
        titles[episode - 1] = title
    return [{"season": season, "episode": e, "title": t} for e, t in enumerate(titles, start=1)]


SHOWS = {
    "harbor-lights": {"name": "Harbor Lights", "year": 2016, "episodes":
        _season("hl", 1, 10) + _season("hl", 2, 10) + _season("hl", 3, 10)},
    "star-courier-rin": {"name": "Star Courier Rin", "year": 2019, "episodes":
        [{"season": 0, "episode": 1, "title": "OVA: The Lost Parcel"},
         {"season": 0, "episode": 2, "title": "OVA: Return Route"}]
        + _season("scr", 1, 24) + _season("scr", 2, 24, {5: "Signal Lost"})
        + _season("scr", 3, 13)},
    "quiet-orchard": {"name": "The Quiet Orchard", "year": 2021, "episodes": _season("qo", 1, 8, {
        1: "The Well", 2: "Frost on the Branches", 3: "What the Bees Know", 4: "Grafting",
        5: "The Long Harvest", 6: "The Well's Edge", 7: "Windfall", 8: "Rootstock"})},
    "copper-and-vine": {"name": "Copper & Vine", "year": 2014, "episodes":
        [{"season": 0, "episode": 1, "title": "Christmas at the Vineyard"},
         {"season": 0, "episode": 2, "title": "Behind the Vines"}]
        + _season("cv", 1, 12) + _season("cv", 2, 12)
        + _season("cv", 3, 11, {10: "Reckoning (1)", 11: "Reckoning (2)"})},
    "night-shift-kitchen": {"name": "Night Shift Kitchen", "year": 2020, "episodes":
        _season("nsk", 1, 8) + _season("nsk", 2, 8, {7: "Last Service (1)",
                                                    8: "Last Service (2)"})},
}  # fmt: skip


def title(show: str, season: int, episode: int) -> str:
    for e in SHOWS[show]["episodes"]:
        if e["season"] == season and e["episode"] == episode:
            return e["title"]
    raise KeyError((show, season, episode))


def size(path: str, low: int, high: int) -> int:
    """A stable size in [low, high] MB for a path."""
    h = int(hashlib.sha256(path.encode()).hexdigest()[:8], 16)
    return (low + h % (high - low + 1)) * MB + h % MB


def crc(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:8].upper()


def ep(path: str, low: int = 900, high: int = 1700) -> tuple[str, int]:
    return path, size(path, low, high)


CASES: list[dict] = []


def case(id_: str, tags: list[str], show: str, want: list[str], release: str,
         files: list[tuple[str, int]], expected: str | None, why: str) -> None:  # fmt: skip
    files = sorted(files, key=lambda f: f[0].lower())  # torrents list files by path
    paths = [p for p, _ in files]
    assert len(set(paths)) == len(paths), f"{id_}: duplicate paths"
    if expected is not None:
        assert expected in paths, f"{id_}: expected file {expected!r} isn't in the list"
    CASES.append({"id": id_, "tags": tags, "show": show, "want": want, "release": release,
                  "files": [{"path": p, "size": s} for p, s in files],
                  "expected": None if expected is None else paths.index(expected),
                  "expected_path": expected, "why": why})  # fmt: skip


# -- scene names the regex already handles (controls) --------------------------------------------

HL = "Harbor.Lights"
d = f"{HL}.S01.1080p.WEB-DL.DDP5.1.H.264-NTb"
case(
    "scene-01",
    ["scene", "present"],
    "harbor-lights",
    ["S01E03"],
    d,
    [ep(f"{d}/{HL}.S01E{e:02d}.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv") for e in range(1, 11)],
    f"{d}/{HL}.S01E03.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv",
    "S01E03 in the name.",
)
d = "harbor.lights.s02.720p.hdtv.x264-killers"
case(
    "scene-02",
    ["scene", "present"],
    "harbor-lights",
    ["S02E07"],
    d,
    [
        ep(f"{d}/harbor.lights.s02e{e:02d}.720p.hdtv.x264-killers.mkv", 300, 600)
        for e in range(1, 11)
    ],
    f"{d}/harbor.lights.s02e07.720p.hdtv.x264-killers.mkv",
    "Lower-case s02e07.",
)
d = "Harbor Lights (2016) Season 3"
case(
    "scene-03",
    ["scene", "present"],
    "harbor-lights",
    ["S03E10"],
    d,
    [
        ep(f"{d}/Harbor Lights - S03E{e:02d} - {title('harbor-lights', 3, e)}.mkv")
        for e in range(1, 11)
    ],
    f"{d}/Harbor Lights - S03E10 - {title('harbor-lights', 3, 10)}.mkv",
    "S03E10, last file.",
)
d = "Night.Shift.Kitchen.S01.1080p.WEB.h264-EDITH"
files = [
    ep(f"{d}/night.shift.kitchen.s01e{e:02d}.1080p.web.h264-edith.mkv", 700, 1100)
    for e in range(1, 9)
]
files.append((f"{d}/Sample/night.shift.kitchen.s01e04.sample.mkv", size("s", 30, 60)))
case(
    "scene-04",
    ["scene", "present"],
    "night-shift-kitchen",
    ["S01E04"],
    d,
    files,
    f"{d}/night.shift.kitchen.s01e04.1080p.web.h264-edith.mkv",
    "Two files say s01e04; the sample is a twentieth of the size.",
)
d = "Copper.and.Vine.S01.1080p.AMZN.WEB-DL.DDP2.0.H.264-NTG"
case(
    "scene-05",
    ["scene", "present"],
    "copper-and-vine",
    ["S01E12"],
    d,
    [
        ep(f"{d}/Copper.and.Vine.S01E{e:02d}.1080p.AMZN.WEB-DL.DDP2.0.H.264-NTG.mkv")
        for e in range(1, 13)
    ],
    f"{d}/Copper.and.Vine.S01E12.1080p.AMZN.WEB-DL.DDP2.0.H.264-NTG.mkv",
    "S01E12.",
)
d = "Star.Courier.Rin.S02.1080p.CR.WEB-DL.AAC2.0.H.264-VARYG"
case(
    "scene-06",
    ["scene", "present"],
    "star-courier-rin",
    ["S02E05"],
    d,
    [
        ep(f"{d}/Star.Courier.Rin.S02E{e:02d}.1080p.CR.WEB-DL.AAC2.0.H.264-VARYG.mkv", 1300, 1450)
        for e in range(1, 25)
    ],
    f"{d}/Star.Courier.Rin.S02E05.1080p.CR.WEB-DL.AAC2.0.H.264-VARYG.mkv",
    "An anime, but this release uses season numbering.",
)

d = "Harbor Lights S01 DVDRip"
case(
    "nxnn-01",
    ["nxnn", "present"],
    "harbor-lights",
    ["S01E04"],
    d,
    [
        ep(f"{d}/Harbor Lights 1x{e:02d} - {title('harbor-lights', 1, e)}.avi", 350, 450)
        for e in range(1, 11)
    ],
    f"{d}/Harbor Lights 1x04 - {title('harbor-lights', 1, 4)}.avi",
    "1x04.",
)
d = "copper.and.vine.s02.hdtv-lol"
case(
    "nxnn-02",
    ["nxnn", "present"],
    "copper-and-vine",
    ["S02E11"],
    d,
    [ep(f"{d}/copper.and.vine.2x{e:02d}.hdtv-lol.mkv", 300, 400) for e in range(1, 13)],
    f"{d}/copper.and.vine.2x11.hdtv-lol.mkv",
    "2x11.",
)
d = "Night Shift Kitchen - Season 2"
case(
    "nxnn-03",
    ["nxnn", "present"],
    "night-shift-kitchen",
    ["S02E08"],
    d,
    [
        ep(f"{d}/Night Shift Kitchen - 2x{e:02d} - {title('night-shift-kitchen', 2, e)}.mp4")
        for e in range(1, 9)
    ],
    f"{d}/Night Shift Kitchen - 2x08 - Last Service (2).mp4",
    "2x08.",
)

# -- anime numbering: season 1, where the absolute number is the episode number ----------------

R = "Star Courier Rin"
d = f"[Kaze] {R} (01-12) [1080p]"
case(
    "abs-01",
    ["absolute", "present"],
    "star-courier-rin",
    ["S01E07"],
    d,
    [ep(f"{d}/[Kaze] {R} - {e:02d} [1080p][{crc(f'k{e}')}].mkv", 1300, 1450) for e in range(1, 13)],
    f"{d}/[Kaze] {R} - 07 [1080p][{crc('k7')}].mkv",
    "Fansub batch: '- 07'.",
)
d = "Star_Courier_Rin_[720p]"
case(
    "abs-02",
    ["absolute", "present"],
    "star-courier-rin",
    ["S01E11"],
    d,
    [
        ep(f"{d}/Star_Courier_Rin_{e:02d}_[720p][{crc(f'u{e}')}].mkv", 300, 400)
        for e in range(1, 13)
    ],
    f"{d}/Star_Courier_Rin_11_[720p][{crc('u11')}].mkv",
    "Underscores: '_11_'.",
)
d = f"{R} [BD 1080p]"
case(
    "abs-03",
    ["absolute", "present"],
    "star-courier-rin",
    ["S01E03"],
    d,
    [ep(f"{d}/{R} Ep{e:02d} [BD 1080p].mkv", 1500, 2200) for e in range(1, 25)],
    f"{d}/{R} Ep03 [BD 1080p].mkv",
    "'Ep03'.",
)
d = f"{R} [WEB 1080p]"
case(
    "abs-04",
    ["absolute", "present"],
    "star-courier-rin",
    ["S01E09"],
    d,
    [ep(f"{d}/{R} - E{e:02d} [WEB 1080p].mkv", 1300, 1450) for e in range(1, 13)],
    f"{d}/{R} - E09 [WEB 1080p].mkv",
    "'E09' with no season.",
)
d = f"{R} Batch"
case(
    "abs-05",
    ["absolute", "present"],
    "star-courier-rin",
    ["S01E12"],
    d,
    [ep(f"{d}/{R} {e:03d} [1080p].mkv", 1300, 1450) for e in range(1, 25)],
    f"{d}/{R} 012 [1080p].mkv",
    "Three digits: '012'.",
)
d = f"[Kaze] {R} (01-08) [1080p]"
names = {e: f"{d}/[Kaze] {R} - {e:02d} [1080p][{crc(f'c{e}')}].mkv" for e in range(1, 9)}
names[8] = f"{d}/[Kaze] {R} - 08 [1080p][3D05A1B2].mkv"  # a checksum with "05" in it
case(
    "abs-06",
    ["absolute", "present"],
    "star-courier-rin",
    ["S01E05"],
    d,
    [ep(p, 1300, 1450) for p in names.values()],
    names[5],
    "'- 05'; episode 8's checksum [3D05A1B2] also contains 05.",
)
d = f"[Kaze] {R} (01-12) [1080p] (v2)"
names = {e: f"{d}/[Kaze] {R} - {e:02d}{'v2' if e in (6, 9) else ''} [1080p].mkv"
         for e in range(1, 13)}  # fmt: skip
case(
    "abs-07",
    ["absolute", "present"],
    "star-courier-rin",
    ["S01E06"],
    d,
    [ep(p, 1300, 1450) for p in names.values()],
    names[6],
    "A fixed release: '06v2'.",
)
d = f"[Kaze] {R} (BD 1080p HEVC FLAC)"
files = [
    ep(f"{d}/[Kaze] {R} - {e:02d} (BD 1080p HEVC FLAC) [{crc(f'b{e}')}].mkv", 900, 1200)
    for e in range(1, 13)
]
files += [
    (f"{d}/Extras/[Kaze] {R} - NCOP 01 (BD 1080p).mkv", size("op", 60, 120)),
    (f"{d}/Extras/[Kaze] {R} - NCED 01 (BD 1080p).mkv", size("ed", 60, 120)),
]
case(
    "abs-08",
    ["absolute", "present"],
    "star-courier-rin",
    ["S01E01"],
    d,
    files,
    f"{d}/[Kaze] {R} - 01 (BD 1080p HEVC FLAC) [{crc('b1')}].mkv",
    "Episode 01, not the creditless opening or ending numbered 01.",
)
d = f"{R} (2019) [1080p]"
case(
    "abs-09",
    ["absolute", "present"],
    "star-courier-rin",
    ["S01E02"],
    d,
    [
        ep(f"{d}/{R} - {e:02d} - {title('star-courier-rin', 1, e)}.mkv", 1300, 1450)
        for e in range(1, 13)
    ],
    f"{d}/{R} - 02 - {title('star-courier-rin', 1, 2)}.mkv",
    "'- 02 -' with the title.",
)
d = "[Kaze] Hoshi no Haitatsunin Rin (Star Courier Rin) - Batch [1080p]"
case(
    "abs-10",
    ["absolute", "present"],
    "star-courier-rin",
    ["S01E04"],
    d,
    [
        ep(f"{d}/[Kaze] Hoshi no Haitatsunin Rin - {e:02d} [1080p].mkv", 1300, 1450)
        for e in range(1, 13)
    ],
    f"{d}/[Kaze] Hoshi no Haitatsunin Rin - 04 [1080p].mkv",
    "The Japanese title; the torrent's name says which show it is.",
)

# -- anime numbering across seasons ---------------------------------------------------------------

d = f"[Kaze] {R} (01-48) [1080p]"
batch48 = [ep(f"{d}/[Kaze] {R} - {e:02d} [1080p].mkv", 1300, 1450) for e in range(1, 49)]
case(
    "abc-01",
    ["absolute-cross", "present"],
    "star-courier-rin",
    ["S02E05"],
    d,
    batch48,
    f"{d}/[Kaze] {R} - 29 [1080p].mkv",
    "S02E05 is absolute 29 (season 1 has 24); '- 05' is S01E05.",
)
case(
    "abc-02",
    ["absolute-cross", "present"],
    "star-courier-rin",
    ["S02E24"],
    d,
    batch48,
    f"{d}/[Kaze] {R} - 48 [1080p].mkv",
    "S02E24 is absolute 48.",
)
d = f"[Kaze] {R} (49-61) [1080p]"
case(
    "abc-03",
    ["absolute-cross", "present"],
    "star-courier-rin",
    ["S03E01"],
    d,
    [ep(f"{d}/[Kaze] {R} - {e:02d} [1080p].mkv", 1300, 1450) for e in range(49, 62)],
    f"{d}/[Kaze] {R} - 49 [1080p].mkv",
    "S03E01 is absolute 49.",
)
d = f"{R} S2 [1080p]"
case(
    "abc-04",
    ["absolute-cross", "present"],
    "star-courier-rin",
    ["S02E01"],
    d,
    [ep(f"{d}/{R} S2 - {e:02d} [1080p].mkv", 1300, 1450) for e in range(1, 25)],
    f"{d}/{R} S2 - 01 [1080p].mkv",
    "A season-2-only pack numbered per season: 'S2 - 01'.",
)
d = f"{R} Seasons 1-2"
case(
    "abc-05",
    ["absolute-cross", "present"],
    "star-courier-rin",
    ["S02E05"],
    d,
    [ep(f"{d}/Season {s}/{R} - {e:02d}.mkv", 1300, 1450) for s in (1, 2) for e in range(1, 25)],
    f"{d}/Season 2/{R} - 05.mkv",
    "Season folders, numbered per season.",
)
d = f"{R} 2nd Season [1080p]"
case(
    "abc-06",
    ["absolute-cross", "present"],
    "star-courier-rin",
    ["S02E05"],
    d,
    [ep(f"{d}/{R} 2nd Season - {e:02d} [1080p].mkv", 1300, 1450) for e in range(1, 25)],
    f"{d}/{R} 2nd Season - 05 [1080p].mkv",
    "'2nd Season - 05'.",
)

# -- named by the episode's title only -------------------------------------------------------------

QO = "quiet-orchard"
d = "The Quiet Orchard (2021) Season 1"
orchard = [ep(f"{d}/The Quiet Orchard - {title(QO, 1, e)}.mkv", 1200, 1800) for e in range(1, 9)]
case(
    "title-01",
    ["title-only", "present"],
    QO,
    ["S01E01"],
    d,
    orchard,
    f"{d}/The Quiet Orchard - The Well.mkv",
    "'The Well' (E01), not 'The Well's Edge' (E06).",
)
d = "The Quiet Orchard 1080p"
case(
    "title-02",
    ["title-only", "present"],
    QO,
    ["S01E03"],
    d,
    [ep(f"{d}/{title(QO, 1, e)} (1080p).mkv", 1200, 1800) for e in range(1, 9)],
    f"{d}/What the Bees Know (1080p).mkv",
    "Bare titles.",
)
d = "The Quiet Orchard"
case(
    "title-03",
    ["title-only", "present"],
    QO,
    ["S01E04"],
    d,
    [ep(f"{d}/{e:02d}. {title(QO, 1, e)}.mkv", 1200, 1800) for e in range(1, 9)],
    f"{d}/04. Grafting.mkv",
    "Numbered without a season: '04. Grafting'.",
)


def dotted(t: str) -> str:
    return t.lower().replace("'", "").replace(" ", ".")


d = "the.quiet.orchard.s01.1080p.web.h264-glhf"
case(
    "title-04",
    ["title-only", "present"],
    QO,
    ["S01E06"],
    d,
    [
        ep(f"{d}/the.quiet.orchard.{dotted(title(QO, 1, e))}.1080p.web.h264-glhf.mkv", 1200, 1800)
        for e in range(1, 9)
    ],
    f"{d}/the.quiet.orchard.the.wells.edge.1080p.web.h264-glhf.mkv",
    "Dots and no apostrophe: 'the.wells.edge', not 'the.well'.",
)
d = "Harbor Lights Season 2"
case(
    "title-05",
    ["title-only", "present"],
    "harbor-lights",
    ["S02E03"],
    d,
    [ep(f"{d}/Harbor Lights - {title('harbor-lights', 2, e)}.mkv") for e in range(1, 11)],
    f"{d}/Harbor Lights - {title('harbor-lights', 2, 3)}.mkv",
    "Title only, season in the folder.",
)
d = "Copper & Vine - Season 1"
case(
    "title-06",
    ["title-only", "present"],
    "copper-and-vine",
    ["S01E08"],
    d,
    [ep(f"{d}/Copper & Vine - {title('copper-and-vine', 1, e)}.mkv") for e in range(1, 13)],
    f"{d}/Copper & Vine - {title('copper-and-vine', 1, 8)}.mkv",
    "Title only.",
)
d = "The Quiet Orchard (2021) Season 1 + Extras"
files = list(orchard)
files = [(p.replace("The Quiet Orchard (2021) Season 1", d), s) for p, s in files]
files.append((f"{d}/Windfall - Behind the Scenes.mkv", size("bts", 150, 300)))
case(
    "title-07",
    ["title-only", "present"],
    QO,
    ["S01E07"],
    d,
    files,
    f"{d}/The Quiet Orchard - Windfall.mkv",
    "The episode, not its behind-the-scenes extra.",
)
d = "Night Shift Kitchen S01"
case(
    "title-08",
    ["title-only", "present"],
    "night-shift-kitchen",
    ["S01E06"],
    d,
    [
        ep(f"{d}/Night Shift Kitchen - {title('night-shift-kitchen', 1, e)}.mkv", 700, 1100)
        for e in range(1, 9)
    ],
    f"{d}/Night Shift Kitchen - {title('night-shift-kitchen', 1, 6)}.mkv",
    "Title only.",
)

# -- season folders and other numbering ------------------------------------------------------------

d = "Night Shift Kitchen"
case(
    "folder-01",
    ["folder-season", "present"],
    "night-shift-kitchen",
    ["S02E05"],
    d,
    [ep(f"{d}/Season {s}/Episode {e:02d}.mkv", 700, 1100) for s in (1, 2) for e in range(1, 9)],
    f"{d}/Season 2/Episode 05.mkv",
    "Season 2 folder, 'Episode 05'; Season 1 has one too.",
)
d = "NSK Complete"
case(
    "folder-02",
    ["folder-season", "present"],
    "night-shift-kitchen",
    ["S01E03"],
    d,
    [ep(f"{d}/Season 0{s}/{e:02d}.mkv", 700, 1100) for s in (1, 2) for e in range(1, 9)],
    f"{d}/Season 01/03.mkv",
    "Bare numbers in season folders.",
)
d = "Harbor Lights S1-S3"
case(
    "folder-03",
    ["folder-season", "present"],
    "harbor-lights",
    ["S02E05"],
    d,
    [
        ep(f"{d}/S{s}/E{e} - {title('harbor-lights', s, e)}.mkv")
        for s in (1, 2, 3)
        for e in range(1, 11)
    ],
    f"{d}/S2/E5 - {title('harbor-lights', 2, 5)}.mkv",
    "'S2/E5': the season is the folder.",
)
d = "Harbor.Lights.S02.HDTV.x264-LOL"
case(
    "folder-04",
    ["folder-season", "present"],
    "harbor-lights",
    ["S02E05"],
    d,
    [ep(f"{d}/harbor.lights.2{e:02d}.hdtv-lol.mp4", 300, 400) for e in range(1, 11)],
    f"{d}/harbor.lights.205.hdtv-lol.mp4",
    "Old scene numbering: '205' is S02E05.",
)
d = "Copper and Vine Series 1-3"
case(
    "folder-05",
    ["folder-season", "present"],
    "copper-and-vine",
    ["S03E07"],
    d,
    [
        ep(f"{d}/Series {s}/Episode {e}.mkv")
        for s, n in ((1, 12), (2, 12), (3, 11))
        for e in range(1, n + 1)
    ],
    f"{d}/Series 3/Episode 7.mkv",
    "British 'Series 3' folders.",
)
d = "Night Shift Kitchen S1"
case(
    "folder-06",
    ["folder-season", "present"],
    "night-shift-kitchen",
    ["S01E07"],
    d,
    [ep(f"{d}/Season 1/Night Shift Kitchen 1-{e:02d}.mkv", 700, 1100) for e in range(1, 9)],
    f"{d}/Season 1/Night Shift Kitchen 1-07.mkv",
    "'1-07' in a Season 1 folder.",
)

# -- several episodes per file ---------------------------------------------------------------------

d = "Copper.and.Vine.S01.720p.HDTV"
files = [ep(f"{d}/Copper.and.Vine.S01E01E02.720p.HDTV.mkv", 1400, 1600)]
files += [ep(f"{d}/Copper.and.Vine.S01E{e:02d}.720p.HDTV.mkv", 700, 800) for e in range(3, 13)]
case(
    "multi-01",
    ["multi", "present"],
    "copper-and-vine",
    ["S01E02"],
    d,
    files,
    f"{d}/Copper.and.Vine.S01E01E02.720p.HDTV.mkv",
    "The double-episode file holds E02.",
)
case(
    "multi-02",
    ["multi", "present"],
    "copper-and-vine",
    ["S01E01"],
    d,
    files,
    f"{d}/Copper.and.Vine.S01E01E02.720p.HDTV.mkv",
    "The double-episode file holds E01.",
)
d = "Copper.and.Vine.S02.720p.HDTV"
files = [ep(f"{d}/Copper.and.Vine.S02E{e:02d}.720p.HDTV.mkv", 700, 800) for e in range(1, 9)]
files.append(ep(f"{d}/Copper.and.Vine.S02E09-E10.720p.HDTV.mkv", 1400, 1600))
files += [ep(f"{d}/Copper.and.Vine.S02E{e:02d}.720p.HDTV.mkv", 700, 800) for e in (11, 12)]
case(
    "multi-03",
    ["multi", "present"],
    "copper-and-vine",
    ["S02E10"],
    d,
    files,
    f"{d}/Copper.and.Vine.S02E09-E10.720p.HDTV.mkv",
    "'S02E09-E10' holds E10.",
)
d = f"[Kaze] {R} (01-24) [1080p]"
files = [ep(f"{d}/[Kaze] {R} - {e:02d} [1080p].mkv", 1300, 1450) for e in range(1, 23)]
files.append(ep(f"{d}/[Kaze] {R} - 23-24 [1080p].mkv", 2600, 2900))
case(
    "multi-04",
    ["multi", "present"],
    "star-courier-rin",
    ["S01E24"],
    d,
    files,
    f"{d}/[Kaze] {R} - 23-24 [1080p].mkv",
    "'- 23-24' holds episode 24.",
)

# -- two-part episodes released under one number ---------------------------------------------------

d = "Copper and Vine S03 1080p"
reck = [(f"{d}/Copper and Vine - S03E{e:02d}.mkv", size(f"cv3{e}", 1000, 1400))
        for e in range(1, 10)]  # fmt: skip
reck += [
    (f"{d}/Copper and Vine - S03E10 - Reckoning Part 1.mkv", 1480 * MB),
    (f"{d}/Copper and Vine - S03E10 - Reckoning Part 2.mkv", 1620 * MB),
]
case(
    "part-01",
    ["part", "present"],
    "copper-and-vine",
    ["S03E11"],
    d,
    reck,
    f"{d}/Copper and Vine - S03E10 - Reckoning Part 2.mkv",
    "E11 is 'Reckoning (2)', released as 'S03E10 - Reckoning Part 2'.",
)
case(
    "part-02",
    ["part", "present"],
    "copper-and-vine",
    ["S03E10"],
    d,
    reck,
    f"{d}/Copper and Vine - S03E10 - Reckoning Part 1.mkv",
    "Both parts say S03E10; E10 is Part 1 (the smaller one).",
)
d = "Night Shift Kitchen S02 720p"
last = [(f"{d}/Night Shift Kitchen S02E{e:02d}.mkv", size(f"nsk2{e}", 500, 700))
        for e in range(1, 7)]  # fmt: skip
last += [
    (f"{d}/Night Shift Kitchen S02E07 Last Service Part One.mkv", 640 * MB),
    (f"{d}/Night Shift Kitchen S02E07 Last Service Part Two.mkv", 710 * MB),
]
case(
    "part-03",
    ["part", "present"],
    "night-shift-kitchen",
    ["S02E08"],
    d,
    last,
    f"{d}/Night Shift Kitchen S02E07 Last Service Part Two.mkv",
    "E08 is 'Last Service (2)', released as 'S02E07 ... Part Two'.",
)
case(
    "part-04",
    ["part", "present"],
    "night-shift-kitchen",
    ["S02E07"],
    d,
    last,
    f"{d}/Night Shift Kitchen S02E07 Last Service Part One.mkv",
    "Both say S02E07; E07 is Part One (the smaller one).",
)

# -- specials --------------------------------------------------------------------------------------

d = "Copper & Vine Complete + Specials"
files = [ep(f"{d}/Season 1/Copper & Vine S01E{e:02d}.mkv") for e in range(1, 13)]
files += [
    ep(f"{d}/Specials/Copper & Vine - Christmas Special (2015).mkv", 1200, 1500),
    ep(f"{d}/Specials/Copper & Vine - Behind the Vines.mkv", 700, 900),
]
case(
    "special-01",
    ["special", "present"],
    "copper-and-vine",
    ["S00E01"],
    d,
    files,
    f"{d}/Specials/Copper & Vine - Christmas Special (2015).mkv",
    "S00E01 'Christmas at the Vineyard' is the Christmas special.",
)
case(
    "special-02",
    ["special", "present"],
    "copper-and-vine",
    ["S00E02"],
    d,
    files,
    f"{d}/Specials/Copper & Vine - Behind the Vines.mkv",
    "S00E02 is itself the 'Behind the Vines' documentary.",
)
d = f"[Kaze] {R} OVA [1080p]"
ovas = [ep(f"{d}/[Kaze] {R} - OVA {n} [1080p].mkv", 1300, 1450) for n in (1, 2)]
case(
    "special-03",
    ["special", "present"],
    "star-courier-rin",
    ["S00E01"],
    d,
    ovas,
    f"{d}/[Kaze] {R} - OVA 1 [1080p].mkv",
    "OVA 1.",
)
case(
    "special-04",
    ["special", "present"],
    "star-courier-rin",
    ["S00E02"],
    d,
    ovas,
    f"{d}/[Kaze] {R} - OVA 2 [1080p].mkv",
    "OVA 2.",
)

# -- the episode isn't in the torrent --------------------------------------------------------------

d = f"[Kaze] {R} (01-12) [1080p]"
batch12 = [ep(f"{d}/[Kaze] {R} - {e:02d} [1080p][{crc(f'k{e}')}].mkv", 1300, 1450)
           for e in range(1, 13)]  # fmt: skip
case(
    "absent-01",
    ["absent", "absent"],
    "star-courier-rin",
    ["S01E13"],
    d,
    batch12,
    None,
    "The batch ends at 12.",
)
files = [*batch12, (f"{d}/Extras/[Kaze] {R} - 13 Preview [1080p].mkv", size("pv", 20, 40))]
case(
    "absent-02",
    ["absent", "absent"],
    "star-courier-rin",
    ["S01E13"],
    d,
    files,
    None,
    "Only a preview of episode 13.",
)
d = f"{HL}.S01.1080p.WEB-DL.DDP5.1.H.264-NTb"
hl1 = [ep(f"{d}/{HL}.S01E{e:02d}.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv") for e in range(1, 11)]
case(
    "absent-03", ["absent", "absent"], "harbor-lights", ["S02E01"], d, hl1, None, "A season-1 pack."
)
files = [f for f in hl1 if "S01E05" not in f[0]]
files.append((f"{d}/Sample/{HL}.S01E05.sample.mkv", size("hs", 30, 60)))
case(
    "absent-04",
    ["absent", "absent"],
    "harbor-lights",
    ["S01E05"],
    d,
    files,
    None,
    "E05 is missing; only its sample is there.",
)
d = "Copper.and.Vine.S01.1080p.BluRay.x264-ROVERS"
files = [ep(f"{d}/Copper.and.Vine.S01E{e:02d}.1080p.BluRay.x264-ROVERS.mkv")
         for e in range(1, 13) if e != 4]  # fmt: skip
files.append((f"{d}/Extras/Copper.and.Vine.S01E04.Deleted.Scenes.1080p.mkv", size("ds", 90, 160)))
case(
    "absent-05",
    ["absent", "absent"],
    "copper-and-vine",
    ["S01E04"],
    d,
    files,
    None,
    "E04 is missing; only its deleted scenes are there.",
)
d = "The Quiet Orchard (2021) Season 1"
files = [f for f in orchard if "The Long Harvest" not in f[0]]
files.append((f"{d}/Featurette - Filming The Long Harvest.mkv", size("ft", 150, 300)))
case(
    "absent-06",
    ["absent", "absent"],
    QO,
    ["S01E05"],
    d,
    files,
    None,
    "E05 'The Long Harvest' is missing; only a featurette about it is there.",
)
d = "Night.Shift.Kitchen.S01.1080p.WEB.h264-EDITH"
files = [
    ep(f"{d}/night.shift.kitchen.s01e{e:02d}.1080p.web.h264-edith.mkv", 700, 1100)
    for e in range(1, 8)
]
files.append((f"{d}/Night.Shift.Kitchen.S01.Trailer.1080p.mkv", size("tr", 20, 40)))
case(
    "absent-07",
    ["absent", "absent"],
    "night-shift-kitchen",
    ["S01E08"],
    d,
    files,
    None,
    "The pack has episodes 1-7 and a trailer.",
)
d = f"[Kaze] {R} (01-24) [1080p]"
case(
    "absent-08",
    ["absent", "absent"],
    "star-courier-rin",
    ["S02E05"],
    d,
    [ep(f"{d}/[Kaze] {R} - {e:02d} [1080p].mkv", 1300, 1450) for e in range(1, 25)],
    None,
    "S02E05 is absolute 29; this batch ends at 24 ('- 05' is S01E05).",
)
d = "The Quiet Orchard (2021) Season 1"
files = [f for f in orchard if "The Well's Edge" not in f[0]]
case(
    "absent-09",
    ["absent", "absent"],
    QO,
    ["S01E06"],
    d,
    files,
    None,
    "E06 'The Well's Edge' is missing; 'The Well' is E01.",
)
d = f"[Kaze] {R} Creditless OP & ED [1080p]"
case(
    "absent-10",
    ["absent", "absent"],
    "star-courier-rin",
    ["S01E01"],
    d,
    [
        ep(f"{d}/[Kaze] {R} - NC{kind} {n:02d} [1080p].mkv", 60, 120)
        for kind in ("OP", "ED")
        for n in (1, 2)
    ],
    None,
    "Only creditless openings and endings.",
)


def markdown() -> str:
    lines = ["# Pack-matching eval cases", "",
             "Generated by `make_cases.py`; edit that file, not this one.", "",
             f"{len(CASES)} cases. `expected` is the file that holds the episode, or none.", "",
             "| id | tags | want | expected | why |", "|---|---|---|---|---|"]  # fmt: skip
    for c in CASES:
        expected = c["expected_path"].rsplit("/", 1)[-1] if c["expected_path"] else "**none**"
        lines.append(f"| {c['id']} | {', '.join(c['tags'])} | {' '.join(c['want'])} | "
                     f"{expected.replace('|', '/')} | {c['why']} |")  # fmt: skip
    for c in CASES:
        want = ", ".join(f'{w} "{title(c["show"], int(w[1:3]), int(w[4:]))}"' for w in c["want"])
        lines += ["", f"## {c['id']}", "", f"Show: {SHOWS[c['show']]['name']}; wanted: {want}",
                  f"Torrent: `{c['release']}`", "", "````"]  # fmt: skip
        for i, f in enumerate(c["files"]):
            mark = "  <- expected" if i == c["expected"] else ""
            lines.append(f"{i:>2}  {f['size'] / MB:7.0f} MB  {f['path']}{mark}")
        lines.append("````")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    shows = json.dumps(SHOWS, indent=1) + "\n"
    (HERE / "shows.json").write_text(shows, encoding="utf-8", newline="\n")
    with (HERE / "cases.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for c in CASES:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    (HERE / "CASES.md").write_text(markdown(), encoding="utf-8", newline="\n")
    print(f"{len(CASES)} cases")
