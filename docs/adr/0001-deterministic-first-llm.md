# 0001: The selection parser goes first; the LLM only fills a typed form

Status: accepted (Phase 4)

## Context

cinemind has a small selection language (`s1e1-5 /1080p`, `s1 eps10 - s2 eps 3`, `all /best`).
People also want to type requests such as "the last two seasons of Severance in 4K". An LLM
reads that easily. But letting it produce the episode list or pick files directly has three
problems:

- **Wrong answers that look right.** "The last two seasons" depends on how many seasons have
  aired. The model doesn't know that reliably, and an off-by-one downloads the wrong season.
- **Cost and latency for nothing.** Most input is already in the selection language. A model
  call there adds a second or more, and money, for an answer the parser gives for free.
- **Hard to test.** Free-form output can't be checked against the metadata, or covered by
  ordinary unit tests.

## Decision

1. **Deterministic first.** `parse_selection` always runs first. The model is only called
   when the parser raises `SelectionError` (`ai/intent.py: understand_selection`), or for
   `cinemind ask`, where there's no title to parse against yet.
2. **A typed form, not an answer.** The model returns a pydantic model through structured
   output: `scope` (all, latest season, last or first N seasons, given seasons, or an explicit
   selection), `count`, and `quality`. It never returns episode lists, files, sources or paths.
3. **Resolution is code.** `selection_text` turns the form into selection syntax using the
   title's real, aired season list. Then the same parser validates it, so a season that
   doesn't exist is an error, not a download.
4. **Show the reading.** The CLI prints "Read as `s2-s3 /2160p`" and the MCP plan has a
   `read_as` field, so the user sees, and can reuse, exactly what was understood.
5. **One wrapper for every call** (`ai/llm.py`). It handles structured output, the cacheable
   system prompt, server-side refusal fallback, a typed error chain (`LLMUnavailable` means
   fall back, `LLMError` means report it), and a usage log in `state.db`.

## Consequences

- Requests the parser understands cost nothing and behave the same as before.
- The model can misread a request, but it can't produce an impossible one. The worst case
  is a valid plan for the wrong seasons, which the "Read as" line and the plan's
  confirmation step make visible before anything downloads.
- Requests the form can't express ("every episode with Gunther in it") are out of scope.
  The model can say so by not marking the text as a download request.
- Almost everything is testable without the network. The form-to-selection code is plain
  unit tests, and the wrapper is tested against a fake client that returns or raises the
  SDK's real types. Only prompt quality needs live calls; the evals in Phase 6 cover that.
