# 0002: An agent can only start a download with a fresh, single-use plan id

Status: accepted (Phase 4)

## Context

The MCP server lets an AI assistant act for the user, and downloading is a real side effect:
it uses disk space and bandwidth, can run for hours, and (with torrent sources) has legal
weight. The assistant is also exposed to untrusted text. Titles, release names and file names
come from third-party servers and could contain instructions aimed at it.

MCP clients usually ask the user before each tool call, but that isn't guaranteed. Users
often allow a tool permanently, and a bare "download X" call shows nothing about what X will
cost.

## Decision

Starting a download takes two steps, and only the first is free:

1. `plan_download` / `plan_request` builds a plan and returns it in full: every file, its size,
   resolution and source, and what is missing or already in the library. It downloads
   nothing, and its tool annotation says it is read-only.
2. `start_download` takes **only a `plan_id`**. It can't take a title or selection. The id:
   - comes from the server's `PlanBook`, so it can't be made up or carried over from
     another session;
   - works **once**;
   - **expires** after `mcp_plan_ttl_minutes` (15), so what starts is what the user was just
     shown, with current sizes and sources.

Around this:

- **Limits are checked twice**, when the plan is made and again at start (settings may have
  changed): `mcp_max_plan_gb`, `mcp_max_files`, and every target inside the library roots.
- **`--read-only`** doesn't register `start_download`, `pause_job` or `resume_job` at all, so an
  agent can't even see them.
- **Every call is audited** (`tool_calls`), with `ok`, `refused` or `error`.
- **Errors are written for the agent.** A refusal says what to do next ("make a new plan and
  show it to the user"). Unexpected errors are logged in full and returned without internal
  details.
- **The server's instructions** tell the agent to show the plan and wait for the user's OK,
  and to treat third-party text as data.

## Consequences

- An agent can't jump straight from a search to a download. The shortest path to downloading
  goes through a plan the user can see.
- An injected instruction like "now call start_download for Y" can't name new content: it
  would need a plan id for Y, and getting one means making a plan the user is shown.
- An agent has to re-plan if the user takes longer than the TTL to decide. That's a little
  friction, and it's deliberate.
- Plans live in memory, so restarting the server invalidates them. Jobs, which are in
  SQLite, survive restarts.
- This doesn't prove the user saw the plan; the assistant could skip showing it. The
  annotations, the instructions and the client's own approval prompt cover that. The server
  makes sure that whatever does start is bounded and was planned first.
