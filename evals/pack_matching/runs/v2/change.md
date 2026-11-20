The judge on a local model: `qwen2.5:7b` through Ollama (greedy, 16K context), asked only when the v1 name rules find no file or two that are too close to call. `--judge ollama --model qwen2.5:7b --reps 2 --record`; the replies are in `cassettes/v2.jsonl`.

Correct goes from 33% to 93% (paired +64% ±14% against the baseline). Every absolute, title-only, folder-season, multi-episode and two-part case is now found. But wrong picks double, from 3.3% to 6.6%: four cases, the same answer in both reps, every one at "high" confidence.

- `abc-02`: S02E24 (absolute 48) in a 01-48 batch; picked `- 24`.
- `absent-08`: S02E05 (absolute 29) isn't in a 01-24 batch; picked `- 05`.
- `special-01`: S00E01, a Christmas special; picked `S01E01`.
- `absent-09`: "The Well's Edge" isn't in the pack; picked "The Well".

The model said "high" on 100 of its 102 answers and "low" on 2, never "medium", so `judge_min_confidence` can't separate its right answers from its wrong ones.
