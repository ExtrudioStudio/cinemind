# Architecture decision records

Short records of decisions that shape the code: the context, what was decided, and what it
costs. Each one is the reasoning behind a part of the code that would otherwise look like an
arbitrary choice.

| # | Decision | Status |
|---|---|---|
| [0001](0001-deterministic-first-llm.md) | The selection parser goes first; the LLM only fills a typed form | Accepted |
| [0002](0002-two-phase-mcp-confirmation.md) | An agent can only start a download with a fresh, single-use plan id | Accepted |
| [0003](0003-no-transcoding-on-the-nas.md) | Files are made playable before they reach the library; the server never transcodes | Accepted |
| [0004](0004-port-the-powershell-pipeline.md) | Port the PowerShell pipeline to Python, proven by recorded verdicts | Accepted |
| [0005](0005-recorded-delivery-stages.md) | Every file's delivery is a recorded state machine, reviewed by a person when needed | Accepted |
