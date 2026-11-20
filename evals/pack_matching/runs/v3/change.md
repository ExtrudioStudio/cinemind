Checks on the judge's pick: v2's recorded replies (`cassettes/v2.jsonl`, `qwen2.5:7b`), replayed through `planning.matching.contradiction`. The model and its prompt are unchanged; no model was called. `--replay cassettes/v2.jsonl --reps 2 --checks on`.

A pick is thrown out when its name points at another episode and nothing in it points at the wanted one (a word of its title, its number), and the name rules' answer stands:

- **Another episode code**: `S01E01` picked for S00E01. Multi-episode codes (`S02E09-E10`) count as covering both, and a two-part finale's `S03E10 - Reckoning Part 2` passes for S03E11 "Reckoning (2)" on the title word.
- **A season-relative number with no sign of the season**: `- 24` picked for S02E24 (episode 48 counted across seasons) in a `(01-48)` batch. `S2`, `Season 2`, `Series 2` or `2nd Season` anywhere in the torrent's name or the path makes the number fine.
- **Another episode's full title, and not this one's**: "The Well" picked for "The Well's Edge".

All four of v2's wrong picks are thrown out, and none of its right ones: wrong picks go from 6.6% to 0%, correct from 93% to 97%. Two of the four become correct "none" answers (the episode wasn't there). The other two (S02E24 in a 01-48 batch, the Christmas special) now find no file, and the episode is re-planned, so "right to say none" falls from 100% to 83%.

The checks were written after seeing these four failures, so this run flatters them. They were also unit-tested on names that aren't in the eval (`tests/test_judge.py`), and the self-check's perfect judge still scores 61/61 through them. New cases, written without looking at the checks, would measure them fairly.
