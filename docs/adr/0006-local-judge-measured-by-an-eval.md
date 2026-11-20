# 0006: The pack-file judge runs on a local model, and an eval decides what it's worth

Status: accepted (Phase 6)

## Context

When a torrent's file names don't say which file is which episode, the name rules give up and
the episode is re-planned onto another release, often a worse one. A language model reads
`[Kaze] Star Courier Rin - 29 [1080p].mkv` as S02E05 easily. But this decision happens on every
pack download, mostly unattended, and a wrong answer silently puts the wrong episode in the
library.

## Decision

1. **Name rules first; the judge only for what they can't settle.** That means no file says
   the episode, or two files say it about equally (`Part 1` / `Part 2`). Deterministic
   improvements go into the rules: decoys such as samples and deleted scenes are skipped. This
   follows [ADR 0001](0001-deterministic-first-llm.md).
2. **The judge chooses; it doesn't generate.**
   - The answer's schema is an enum of the ids of the files it was shown, plus `"none"`.
   - Confidence below `judge_min_confidence` counts as no answer.
   - File and torrent names reach it as data inside a JSON document, with `<` escaped.
   - Its pick is checked against the file's name (`matching.contradiction`). A file named for
     another episode is thrown out, and the answer counts as none: another episode code, a
     season-relative number with no sign of that season, or another episode's title.
3. **Local by default.** `judge_provider = "ollama"` runs `qwen2.5:7b` on the machine. That's
   free, offline, and keeps file names private. If it isn't installed, the judge abstains and
   behaviour is exactly the name rules. Claude (`anthropic`) is one setting away, behind the
   same `Backend` contract.
4. **An eval decides, before and after every change.** 61 reviewed cases run through the
   production function (`matching.resolve`). Wrong picks are graded separately from misses.
   Infrastructure failures are kept out of the scores, and the grading is checked with an
   oracle, null inputs and induced errors. Model runs need a person to approve the harness
   first. See [docs/evals.md](../evals.md).

## Consequences

- The eval paid for itself before any model ran. Its self-check showed that a perfect judge
  could reach only 59/61, because the name rules picked a lone sample first. The decoy rule
  that fixed this is deterministic and free, and it halved wrong picks.
- Whether the local model is good enough is a number in `docs/evals.md`, not an assumption.
  The same goes for whether Claude is worth its cost here.
- The local model's confidence turned out to be no use as a filter. It said "high" on 100 of
  its 102 answers, including all four wrong picks, so `judge_min_confidence = "high"` would
  have changed nothing. What did work was deterministic again: checking the pick against what
  the file's name already says. On the recorded replies that took wrong picks from 6.6% to 0%
  and correct from 93% to 97%, measured by replay with no model call.
- The engine waits for the judge only for the files it asked about. The others download
  meanwhile, and a torrent never finishes while a match is pending.
