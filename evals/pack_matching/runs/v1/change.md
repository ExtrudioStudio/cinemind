Name rules skip decoys: samples, trailers, previews, extras, deleted scenes, behind-the-scenes and creditless openings/endings named with the episode's code.

Before, a lone `Sample/show.s01e05.sample.mkv` (or `S01E04.Deleted.Scenes`) was picked as the episode when the real file was missing, so the wrong video was downloaded. Only folders inside the torrent and the text after the episode code are read, so shows named like a decoy ("Trailer Park Boys", "Extras") still match. No model; `--name-rules current` (baseline ran with `legacy`).
