"""Run the pack-matching eval: the name rules, then (optionally) the judge, on every case.

    uv run python evals/pack_matching/run.py --variant baseline                  # name rules (free)
    uv run python evals/pack_matching/run.py --variant v1 --judge ollama --model qwen2.5:7b --reps 2
    uv run python evals/pack_matching/run.py --variant v2 --judge anthropic \\
        --model claude-opus-5-5 --effort low --reps 2 --record
    uv run python evals/pack_matching/run.py --summary                           # all variants
    uv run python evals/pack_matching/run.py --self-check                        # test the grading

The cases are ``cases.jsonl`` (reviewed; regenerate with ``make_cases.py``). Each case runs
through :func:`cinemind.planning.matching.resolve`, the code the engine uses, so the eval
measures the product, not a copy of it. A case is graded on the file it ends up with:

- ``correct``: the right file, or no file when the episode isn't in the torrent (headline);
- ``wrong_pick``: a file that isn't the answer: the wrong episode gets downloaded (lower is
  better, and the one that matters most);
- ``abstained``: no file (the episode gets re-planned).

Output, one directory per variant under ``runs/`` (``baseline``, ``v1``, ``v2``, ...):
``results.jsonl`` (a row per case and rep, written as each finishes), ``traces/<id>_rep<k>.json``
(what the judge was sent and answered) and ``errors.jsonl`` (attempts that produced nothing to
grade: no model reachable, timeouts, a different model answering). A rerun resumes: rows that
exist are skipped. Build the report with the claude-api skill's ``build-report-lite.mjs runs/``.

Runs that call a model refuse to start until the harness (this file, the cases and the judge's
code, see ``harness_paths`` in ``runs/_state.json``) matches the sha a person approved with
``--approve-harness``. That flag is for a person to pass after reviewing a change.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from cinemind.ai.judge import SYSTEM, PackJudge, choice_model
from cinemind.ai.llm import LLM, CallRecord, LLMError, LLMUnavailable
from cinemind.ai.ollama import OllamaLLM
from cinemind.core.config import Config
from cinemind.core.models import Episode, EpisodeRef, Title
from cinemind.planning.matching import Located, PackFile, Wanted, resolve

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
FLOW = HERE / "runs"
VARIANT_NAME = "baseline or v<N>"
TRANSIENT_ATTEMPTS = 4


# -- cases -----------------------------------------------------------------------------------------


def load_cases(path: Path = HERE / "cases.jsonl") -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def load_titles(path: Path = HERE / "shows.json") -> dict[str, Title]:
    shows = json.loads(path.read_text(encoding="utf-8"))
    return {slug: Title("tt0000000", "series", s["name"], s["year"],
                        episodes=[Episode(e["season"], e["episode"], e["title"])
                                  for e in s["episodes"]])
            for slug, s in shows.items()}  # fmt: skip


def wanted_for(case: dict[str, Any], titles: dict[str, Title]) -> Wanted:
    refs = [EpisodeRef(int(code[1:3]), int(code[4:])) for code in case["want"]]
    return Wanted.from_title(titles[case["show"]], refs, case["release"])


def files_for(case: dict[str, Any]) -> list[PackFile]:
    return [PackFile(i, f["path"], f["size"]) for i, f in enumerate(case["files"])]


def describe(case: dict[str, Any], wanted: Wanted) -> str:
    parts = [
        f'{r.code} "{t}"' + (f" (absolute {a})" if a else "")
        for r, t, a in zip(wanted.refs, wanted.titles, wanted.absolute, strict=True)
    ]
    return (f"{wanted.show}: {', '.join(parts)}. Torrent: {case['release']} "
            f"({len(case['files'])} files)")  # fmt: skip


# -- judges for each mode --------------------------------------------------------------------------


class Scripted:
    """A backend that answers from a function of the case (self-checks) or a cassette (replay)."""

    def __init__(self, answer: Any, model: str = "scripted"):
        self.answer, self.label = answer, model

    async def parse(self, purpose: str, system: str, user: str, output: type,
                    max_tokens: int = 2048, *, calls: list[CallRecord] | None = None,
                    ) -> Any:  # fmt: skip
        reply = self.answer(system, user)
        if isinstance(reply, Exception):
            raise reply
        text, model, usage = reply
        if calls is not None:
            calls.append(CallRecord(model=model, system=system, user=user, text=text,
                                    stop_reason=usage.get("stop_reason", "end_turn"),
                                    input_tokens=usage.get("input_tokens", 0),
                                    output_tokens=usage.get("output_tokens", 0)))  # fmt: skip
        try:
            return output.model_validate_json(text)
        except ValidationError as exc:  # as the real backends report it
            raise LLMError("the answer didn't fit the schema") from exc


def _prompt_sha(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}\n\x00\n{user}".encode()).hexdigest()


def cassette_backend(path: Path, case_id: str, rep: int) -> Scripted:
    recorded = {(r["case"], r["rep"]): r for r in map(json.loads, path.read_text(
        encoding="utf-8").splitlines())}  # fmt: skip

    def answer(system: str, user: str) -> Any:
        r = recorded.get((case_id, rep))
        if r is None:
            return LLMUnavailable(f"no recorded reply for {case_id} rep {rep}")
        if r["prompt_sha"] != _prompt_sha(system, user):
            return LLMUnavailable(f"the recorded reply for {case_id} is for a different prompt")
        return r["text"], r["model"], {**r["usage"], "stop_reason": r["stop_reason"]}

    return Scripted(answer, "replay")


def _json(file: str, confidence: str = "high") -> str:
    return json.dumps({"file": file, "confidence": confidence, "reason": "scripted"})


@dataclass
class Shared:
    args: argparse.Namespace
    client: Any = None  # one Anthropic client for every case
    cassette_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def make_judge(shared: Shared, case: dict[str, Any], rep: int) -> PackJudge | None:
    a = shared.args
    if a.judge == "none":
        return None
    if a.judge == "ollama":
        return PackJudge(OllamaLLM(a.ollama_url, a.model, timeout=a.timeout_s or 120.0),
                         a.min_confidence)  # fmt: skip
    if a.judge == "anthropic":
        cfg = Config(llm_model=a.model, llm_effort=a.effort)
        return PackJudge(LLM(cfg, shared.client), a.min_confidence, a.model)
    if a.judge == "replay":
        return PackJudge(cassette_backend(Path(a.replay), case["id"], rep), a.min_confidence)
    if a.judge == "oracle":  # knows the answer: the pipeline's ceiling
        expected = case["expected"]
        file = "none" if expected is None else str(expected)
        return PackJudge(Scripted(lambda s, u: (_json(file), "oracle", {}), "oracle"))
    raise SystemExit(f"unknown judge {a.judge!r}")


def requested_model(a: argparse.Namespace) -> str | None:
    return {"ollama": f"ollama/{a.model}", "anthropic": a.model}.get(a.judge)


def served_ok(served: str, requested: str | None) -> bool:
    """The answering model is the one asked for (allowing alias -> dated snapshot)."""
    if requested is None:
        return True
    return served == requested or served.startswith((f"{requested}-", f"{requested}@"))


def transient(exc: BaseException | None) -> bool:
    """Worth retrying after a pause: rate limits, overload, 5xx, timeouts, dropped connections."""
    cause = getattr(exc, "__cause__", None)
    try:
        import anthropic

        if isinstance(cause, anthropic.RateLimitError | anthropic.APIConnectionError):
            return True
        if isinstance(cause, anthropic.APIStatusError) and cause.status_code >= 500:
            return True
    except ImportError:
        pass
    import httpx

    return isinstance(cause, httpx.TimeoutException)


# -- one case --------------------------------------------------------------------------------------


class Tracked:
    """The judge for one attempt, remembering whether it was asked and what it was sent."""

    def __init__(self, judge: PackJudge | None):
        self.judge, self.asked = judge, False
        self.calls: list[CallRecord] = []

    async def __call__(self, wanted: Wanted, files: list[PackFile]) -> Located | None:
        self.asked = True
        assert self.judge is not None
        return await self.judge(wanted, files, calls=self.calls)


def grade(picked: int | None, expected: int | None) -> dict[str, int]:
    return {"correct": int(picked == expected),
            "wrong_pick": int(picked is not None and picked != expected),
            "abstained": int(picked is None)}  # fmt: skip


async def run_case(
    shared: Shared,
    case: dict[str, Any],
    rep: int,
    titles: dict[str, Title],
    make: Any = make_judge,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[dict]]:
    """(results row, error row, trace) for one (case, rep); exactly one of row / error is set."""
    a = shared.args
    wanted, files = wanted_for(case, titles), files_for(case)
    retries = 0
    while True:
        judge = make(shared, case, rep)
        tracked = Tracked(judge)
        started = time.monotonic()
        try:
            matching = resolve(wanted, files, tracked if judge else None,
                               skip_decoys=a.name_rules == "current")  # fmt: skip
            got = await asyncio.wait_for(matching, a.timeout_s or None)
        except TimeoutError:  # the ceiling also ends retries; the call may still finish unseen
            return None, _error(case, rep, "timeout", f"over {a.timeout_s}s", retries), []
        latency = round(time.monotonic() - started, 3)
        call = tracked.calls[-1] if tracked.calls else None
        failure = judge.last_error if judge else None
        if tracked.asked and call is None:  # no completed call: nothing the model said to grade
            if transient(failure) and retries + 1 < TRANSIENT_ATTEMPTS:
                retries += 1
                await asyncio.sleep(min(30.0, 2**retries) * random.uniform(0.5, 1.5))
                continue
            return None, _error(case, rep, "serving", str(failure), retries), []
        break

    if call is not None and not served_ok(call.model, requested_model(a)):
        return None, _error(case, rep, "served_model_mismatch",
                            f"asked {requested_model(a)}, answered by {call.model}", retries,
                            call), []  # fmt: skip
    if call is None:
        judge_status = "not_asked"
    elif call.stop_reason in ("max_tokens", "length"):
        judge_status = "truncated"
    elif call.stop_reason == "refusal":
        judge_status = "refused"
    elif failure is not None:
        judge_status = "bad_format"
    elif got.how == "judge":
        judge_status = "answered"
    else:
        judge_status = "unsure"  # below judge_min_confidence: the name rules' answer stands
    picked, expected = got.index, case["expected"]

    def path(i: int | None) -> str | None:
        return None if i is None else case["files"][i]["path"]

    row = {
        "prompt_id": case["id"], "rep": rep, "prompt": describe(case, wanted), "tags": case["tags"],
        "status": "truncated" if judge_status == "truncated" else "ok",
        "stop_reason": call.stop_reason if call else "not_asked",
        "grade": grade(picked, expected),
        "model": call.model if call else "name-rules",
        "usage": {"input_tokens": call.input_tokens if call else 0,
                  "output_tokens": call.output_tokens if call else 0,
                  "cache_read_input_tokens": call.cache_read_tokens if call else 0,
                  "cache_creation_input_tokens": call.cache_write_tokens if call else 0},
        "latency_s": call.latency_s if call else latency, "retries": retries,
        "meta": {"picked": picked, "picked_path": path(picked), "expected": expected,
                 "expected_path": case["expected_path"], "how": got.how,
                 "judge_status": judge_status, "reason": got.reason,
                 "name_rules": a.name_rules},
    }  # fmt: skip
    if call is not None:
        trace = [{"role": "system", "content": call.system},
                 {"role": "user", "content": call.user},
                 {"role": "assistant", "content": call.text}]  # fmt: skip
    else:
        listing = "\n".join(f"{i}: {f['path']}" for i, f in enumerate(case["files"]))
        trace = [
            {"role": "user", "content": f"{describe(case, wanted)}\n\n{listing}"},
            {"role": "assistant", "content": f"Name rules: {got.how}, file {picked}"},
        ]
    if call is not None and a.record:
        entry = {"case": case["id"], "rep": rep, "prompt_sha": _prompt_sha(call.system, call.user),
                 "model": call.model, "text": call.text, "stop_reason": call.stop_reason,
                 "usage": {"input_tokens": call.input_tokens,
                           "output_tokens": call.output_tokens}}  # fmt: skip
        async with shared.cassette_lock:
            with (HERE / "cassettes" / f"{a.variant}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return row, None, trace


def _error(case: dict[str, Any], rep: int, cls: str, detail: str, retries: int,
           call: CallRecord | None = None) -> dict[str, Any]:  # fmt: skip
    row: dict[str, Any] = {"prompt_id": case["id"], "rep": rep, "class": cls,
                           "detail": detail[:500], "retries": retries}  # fmt: skip
    if call is not None:  # billed, so it counts as spend
        row["model"] = call.model
        row["usage"] = {"input_tokens": call.input_tokens, "output_tokens": call.output_tokens}
    return row


# -- the run ---------------------------------------------------------------------------------------


def _append(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _done(path: Path) -> set[tuple[str, int]]:
    if not path.exists():
        return set()
    return {(r["prompt_id"], r["rep"]) for r in map(json.loads, path.read_text(
        encoding="utf-8").splitlines()) if r}  # fmt: skip


async def run(args: argparse.Namespace, flow: Path, cases: list[dict[str, Any]]) -> None:
    vdir = flow / args.variant
    (vdir / "traces").mkdir(parents=True, exist_ok=True)
    if args.record:
        (HERE / "cassettes").mkdir(exist_ok=True)
    titles = load_titles()
    shared = Shared(args)
    if args.judge == "anthropic":
        import anthropic

        shared.client = anthropic.AsyncAnthropic(max_retries=2, timeout=60.0)
    done = _done(vdir / "results.jsonl")
    todo = [(c, r) for c in cases for r in range(args.reps) if (c["id"], r) not in done]
    gate = asyncio.Semaphore(args.concurrency)
    started, finished, errors = time.monotonic(), 0, 0

    async def one(case: dict[str, Any], rep: int) -> None:
        nonlocal finished, errors
        async with gate:
            row, err, trace = await run_case(shared, case, rep, titles)
        if err is not None:
            errors += 1
            _append(vdir / "errors.jsonl", err)
            return
        assert row is not None
        (vdir / "traces" / f"{case['id']}_rep{rep}.json").write_text(
            json.dumps(trace, ensure_ascii=False, indent=1), encoding="utf-8")  # fmt: skip
        _append(vdir / "results.jsonl", row)
        finished += 1

    await asyncio.gather(*(one(c, r) for c, r in todo))
    print(
        f"{args.variant}: {finished} case-rep(s) graded, {errors} error(s), "
        f"{len(done)} already done; {time.monotonic() - started:.1f}s",
        file=sys.stderr,
    )


# -- harness approval ------------------------------------------------------------------------------


def _harness_sha(state: dict[str, Any]) -> str:
    h = hashlib.sha256()
    paths = [Path(__file__).resolve(), REPO / "uv.lock", HERE / "cases.jsonl", HERE / "shows.json",
             *(REPO / p for p in state.get("harness_paths", []))]  # fmt: skip
    for p in paths:
        h.update(str(p.relative_to(REPO)).encode() + b"\0")
        h.update(p.read_bytes() if p.is_file() else b"<missing>")
    return h.hexdigest()


def check_harness(flow: Path, approve: bool) -> None:
    state_path = flow / "_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    sha = _harness_sha(state)
    if state.get("harness_sha") == sha:
        return
    if approve:
        state["harness_sha"] = sha
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        print(f"harness approved: sha256 {sha[:12]} recorded in {state_path}", file=sys.stderr)
        return
    print(f"The harness changed since it was last approved (now {sha[:12]}). Review the change, "
          "then run once with --approve-harness.", file=sys.stderr)  # fmt: skip
    raise SystemExit(2)


# -- summary ---------------------------------------------------------------------------------------


def _rows(vdir: Path, name: str) -> list[dict[str, Any]]:
    p = vdir / name
    return (
        [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]
        if p.exists()
        else []
    )


def _ci(values: list[float]) -> tuple[float, float]:
    """Mean and the half-width of its 95% interval (normal approximation over cases)."""
    m = statistics.fmean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    return m, 1.96 * sd / math.sqrt(len(values))


def summarize(flow: Path) -> str:
    state = json.loads((flow / "_state.json").read_text(encoding="utf-8"))
    prices = state.get("prices", {})
    variants = sorted((d for d in flow.iterdir() if d.is_dir() and (d.name == "baseline" or (
        d.name[:1] == "v" and d.name[1:].isdigit()))),
        key=lambda d: -1 if d.name == "baseline" else int(d.name[1:]))  # fmt: skip
    per_variant: dict[str, dict[str, dict[str, float]]] = {}
    out = ["| variant | judge | correct (95% CI) | wrong picks | found (present) | right to say "
           "none | errors | median tokens in/out | p50 latency | cost / run |",
           "|---|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for vdir in variants:
        rows = [r for r in _rows(vdir, "results.jsonl") if r.get("status", "ok") == "ok"]
        errs = _rows(vdir, "errors.jsonl")
        if not rows:
            continue
        cases: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            cases[r["prompt_id"]].append(r)
        means = {cid: {m: statistics.fmean(r["grade"][m] for r in rs)
                       for m in ("correct", "wrong_pick", "abstained")}
                 for cid, rs in cases.items()}  # fmt: skip
        per_variant[vdir.name] = means
        tags = {cid: rs[0]["tags"] for cid, rs in cases.items()}
        correct, half = _ci([v["correct"] for v in means.values()])
        wrong = statistics.fmean(v["wrong_pick"] for v in means.values())
        present = [v["correct"] for cid, v in means.items() if tags[cid][1] == "present"]
        nones = [r for r in rows if r["grade"]["abstained"]]
        none_ok = statistics.fmean(r["grade"]["correct"] for r in nones) if nones else float("nan")
        asked = [r for r in rows if r["model"] != "name-rules"]
        tin = statistics.median(r["usage"]["input_tokens"] for r in asked) if asked else 0
        tout = statistics.median(r["usage"]["output_tokens"] for r in asked) if asked else 0
        p50 = statistics.median(r["latency_s"] for r in asked) if asked else 0.0
        models = sorted({r["model"] for r in rows} - {"name-rules"}) or ["(name rules only)"]
        cost = 0.0
        priced = True
        for r in [*rows, *errs]:
            if "usage" not in r or r.get("model") in (None, "name-rules"):
                continue
            price = prices.get(r["model"])
            if price is None:
                priced = False
                continue
            u = r["usage"]
            cost += (
                u.get("input_tokens", 0) * price["in"]
                + u.get("output_tokens", 0) * price["out"]
                + u.get("cache_read_input_tokens", 0) * price["in"] * 0.1
                + u.get("cache_creation_input_tokens", 0) * price["in"] * 1.25
            ) / 1e6
        reps = max(len(rs) for rs in cases.values())
        if not asked:
            spend = "no model calls"
        elif all(m.startswith("ollama/") for m in models):
            spend = "local, no API bill"
        else:
            spend = f"${cost:.3f}" if priced else "not measured"
        out.append(
            f"| {vdir.name} | {', '.join(models)} | {correct:.0%} ±{half:.0%} ({len(means)} cases"
            f" x {reps}) | {wrong:.1%} | {statistics.fmean(present):.0%} | "
            f"{'-' if not nones else f'{none_ok:.0%}'} | {len(errs)} | "
            f"{tin:.0f}/{tout:.0f} | {p50:.2f}s | {spend} |")  # fmt: skip
    if "baseline" in per_variant and len(per_variant) > 1:
        out += ["", "Paired difference in `correct` against the baseline (per case, 95% CI):", ""]
        base = per_variant["baseline"]
        for name, means in per_variant.items():
            if name == "baseline":
                continue
            diffs = [means[c]["correct"] - base[c]["correct"] for c in means if c in base]
            m, half = _ci(diffs)
            verdict = "significant" if abs(m) > half else "within noise"
            out.append(f"- {name}: {m:+.0%} ±{half:.0%} over {len(diffs)} cases ({verdict})")
    by_tag: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for name, means in per_variant.items():
        rows = _rows(flow / name, "results.jsonl")
        tag_of = {r["prompt_id"]: r["tags"][0] for r in rows}
        for cid, v in means.items():
            by_tag[tag_of[cid]][name].append(v["correct"])
    names = list(per_variant)
    out += ["", "Correct by kind of case:", "", "| kind | cases | " + " | ".join(names) + " |",
            "|---|---|" + "---|" * len(names)]  # fmt: skip
    for tag, cols in by_tag.items():
        n = len(next(iter(cols.values())))
        out.append(
            f"| {tag} | {n} | "
            + " | ".join(f"{statistics.fmean(cols[v]):.0%}" if v in cols else "-" for v in names)
            + " |"
        )
    return "\n".join(out)


# -- self-check ------------------------------------------------------------------------------------


def self_check(cases: list[dict[str, Any]]) -> int:
    """Push known answers through the grading and the error handling; return failures."""
    failures = 0

    def check(name: str, ok: bool, detail: str) -> None:
        nonlocal failures
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")

    n = len(cases)
    absent = sum(c["expected"] is None for c in cases)
    oracle = [grade(c["expected"], c["expected"]) for c in cases]
    check("oracle output", sum(g["correct"] for g in oracle) == n, f"{n}/{n} correct expected")
    null = [grade(None, c["expected"]) for c in cases]
    check("empty output", sum(g["correct"] for g in null) == absent
          and not any(g["wrong_pick"] for g in null),
          f"only the {absent} absent cases count as correct, no wrong picks")  # fmt: skip
    anti = [grade((c["expected"] + 1) % len(c["files"]) if c["expected"] is not None else 0,
                  c["expected"]) for c in cases]  # fmt: skip
    check("always-wrong output", not any(g["correct"] for g in anti)
          and all(g["wrong_pick"] for g in anti), "0 correct, every case a wrong pick")  # fmt: skip

    with tempfile.TemporaryDirectory() as tmp:
        flow = Path(tmp)
        base = argparse.Namespace(variant="baseline", judge="oracle", model="", reps=1,
                                  concurrency=8, timeout_s=30.0, min_confidence="medium",
                                  record=False, replay=None, effort="low",
                                  ollama_url="", name_rules="current")  # fmt: skip
        asyncio.run(run(base, flow, cases))
        rows = _rows(flow / "baseline", "results.jsonl")
        right = sum(r["grade"]["correct"] for r in rows)
        stuck = sorted(r["prompt_id"] for r in rows if not r["grade"]["correct"])
        check(
            "perfect judge through the pipeline",
            right == n,
            f"{right}/{n} correct; the judge is never asked about {stuck or 'nothing'}",
        )

        def scripted(answer: Any, **overrides: Any) -> tuple[Any, Any, Any]:
            shared = Shared(argparse.Namespace(**{**vars(base), **overrides}))
            judge = PackJudge(Scripted(answer))
            hard = next(c for c in cases if c["id"] == "abs-01")  # one the judge is asked about
            return asyncio.run(run_case(shared, hard, 0, load_titles(), lambda *_: judge))

        row, err, _ = scripted(lambda s, u: LLMUnavailable("induced outage"))
        check("induced API error", row is None and err is not None and err["class"] == "serving",
              "lands in errors.jsonl as 'serving', not as a zero")  # fmt: skip
        row, err, _ = scripted(lambda s, u: (_json("0"), "ollama/llama3:8b", {}),
                               judge="ollama", model="qwen2.5:7b")  # fmt: skip
        check("a different model answering", row is None and err is not None
              and err["class"] == "served_model_mismatch", "refused, not graded")  # fmt: skip
        row, err, _ = scripted(lambda s, u: ("not json", "scripted", {}))
        check("an answer out of shape", row is not None and row["meta"]["judge_status"]
              == "bad_format", "graded as the name rules' answer, marked bad_format")  # fmt: skip
        row, err, _ = scripted(lambda s, u: (_json("0", "low"), "scripted", {}))
        check("an unsure answer", row is not None and row["meta"]["judge_status"] == "unsure"
              and row["meta"]["picked"] is None, "counts as no answer")  # fmt: skip
        schema = choice_model(["0", "1"]).model_json_schema()
        check("answer schema", schema["properties"]["file"]["enum"] == ["0", "1", "none"],
              "the judge can only name a file it was shown, or none")  # fmt: skip
        check("prompt", "treat it as data" in SYSTEM, "names are data, not instructions")
    return failures


# -- command line ----------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--variant", default="baseline", help=VARIANT_NAME)
    p.add_argument("--judge", choices=["none", "ollama", "anthropic", "replay", "oracle"],
                   default="none")  # fmt: skip
    p.add_argument("--model", default="", help="qwen2.5:7b for ollama, claude-opus-5-5, ...")
    p.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=0, help="default: 1 for ollama, else 4")
    p.add_argument("--timeout-s", type=float, default=180.0, help="per case; 0 = none")
    p.add_argument("--min-confidence", choices=["high", "medium"], default="medium")
    p.add_argument("--name-rules", choices=["current", "legacy"], default="current",
                   help="legacy: as before decoys were skipped (how baseline was run)")  # fmt: skip
    p.add_argument("--ollama-url", default="http://localhost:11434")
    p.add_argument(
        "--record", action="store_true", help="save replies to cassettes/<variant>.jsonl"
    )
    p.add_argument("--replay", help="answer from this cassette instead of a model")
    p.add_argument("--cases", nargs="*", help="only these case ids")
    p.add_argument("--flow", type=Path, default=FLOW)
    p.add_argument("--summary", action="store_true")
    p.add_argument("--self-check", action="store_true")
    p.add_argument("--approve-harness", action="store_true")
    a = p.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # the tables use ±
    cases = load_cases()
    if a.self_check:
        return 1 if self_check(cases) else 0
    if a.summary:
        print(summarize(a.flow))
        return 0
    if a.approve_harness:  # a person's step: record the reviewed harness, run nothing
        check_harness(a.flow, approve=True)
        return 0
    if not (a.variant == "baseline" or (a.variant[:1] == "v" and a.variant[1:].isdigit())):
        p.error(f"--variant must be {VARIANT_NAME} (the report ignores other names)")
    if a.replay:
        a.judge = "replay"
    if a.judge in ("ollama", "anthropic") and not a.model:
        p.error("--model is required with --judge ollama/anthropic")
    a.concurrency = a.concurrency or (1 if a.judge == "ollama" else 4)
    if a.judge in ("ollama", "anthropic"):  # model calls: the harness must be approved
        check_harness(a.flow, approve=False)
    if a.cases:
        cases = [c for c in cases if c["id"] in set(a.cases)]
    asyncio.run(run(a, a.flow, cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
