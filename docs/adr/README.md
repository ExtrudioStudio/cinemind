# Architecture decision records

Short records of decisions that shape the code: the context, what was decided, and what it
costs. Each one is the reasoning behind a part of the code that would otherwise look like an
arbitrary choice.

| # | Decision | Status |
|---|---|---|
| [0001](0001-deterministic-first-llm.md) | The selection parser goes first; the LLM only fills a typed form | Accepted |
| [0002](0002-two-phase-mcp-confirmation.md) | An agent can only start a download with a fresh, single-use plan id | Accepted |
