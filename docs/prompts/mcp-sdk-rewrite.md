# Prompt: rewrite `mcp/` on the official MCP Python SDK, deployable on Cloudflare

> Paste everything below this line into a fresh Claude Code session on
> `flop-labs/technocore-chat`.

---

Rewrite the MCP server in `mcp/` to use the official MCP Python SDK instead of the
hand-rolled wire protocol, keeping it the thin shim over the HTTP API that it is, and give
it a streamable-HTTP transport that can be deployed as a remote MCP server on Cloudflare.

## Context — read these before writing anything

- `mcp/src/technocore_mcp/protocol.py` — the hand-rolled MCP implementation: JSON-RPC
  framing over stdio, `initialize`/`ping`/`tools/list`/`tools/call`, batch handling, and a
  small schema generator that derives each tool's `inputSchema` from its handler's
  signature (`Annotated` strings become descriptions). This whole file is what the SDK
  replaces.
- `mcp/src/technocore_mcp/server.py` — nine tool handlers. Each one builds a URL, does one
  GET against a technocore-chat instance, and returns the `text/plain` body verbatim. This
  layer survives the rewrite nearly unchanged; only the decorator/registration around it
  changes.
- `mcp/README.md`, the `protocol.py` module docstring, and the comments in
  `mcp/pyproject.toml` all argue the case for **zero dependencies** ("why not the SDK").
  That decision is now deliberately reversed: the hand-rolled protocol has become a steady
  source of spec-conformance bug reports (see the issue list below), and the goal of a
  remote server on Cloudflare needs streamable HTTP and spec-currency that are not worth
  re-implementing by hand. Every sentence in the tree that argues the old case must be
  updated — a docstring stating the opposite of the code is worse than no docstring.
- `AGENTS.md` — the exact commands CI runs; run them before pushing. Note the core-size
  caps do not cover `mcp/` (it is "extra", not core).
- `tests/test_mcp.py`, `tests/test_mcp_falsey_params.py`, `tests/test_mcp_package.py`,
  `tests/verify_mcp_dist.py` — current coverage; see "Tests" below.
- `.github/workflows/publish-mcp.yml` — tag-driven PyPI + MCP-registry publish, with a
  version-lockstep check across `server.py`'s `VERSION`, `mcp/server.json` (twice), and the
  root `pyproject.toml`. Keep that lockstep working.

## What to build

### 1. The SDK replaces `protocol.py`

Use the official Python SDK (`mcp` on PyPI, <https://github.com/modelcontextprotocol/python-sdk>)
with its `FastMCP` server API. Delete `protocol.py`. Preserve, exactly:

- the nine tools — names, descriptions, parameter names, optionality and defaults, and the
  per-parameter descriptions the model reads (these move from `Annotated[..., "text"]`
  strings into `pydantic.Field(description=...)` or whatever the SDK's current idiom is);
- the `instructions` block sent on `initialize`, and `serverInfo` reporting `VERSION`;
- **text out, not JSON**: every tool returns the service's `text/plain` rendering
  untouched. The rendering carries the untrusted-content banner and the `next:` cursor
  line; re-serialising would strip the framing that matters.
- **error bodies are the payload**: the service puts the actionable part of every failure
  in the body (retry delay on 429, current value on 409, the lane that would have worked
  on 403). A failed GET must surface that body text as an `isError` tool result the model
  can act on — never a bare `HTTP Error 429`, never a JSON-RPC error, never a swallowed
  stack trace. Verify how the SDK renders raised exceptions and make sure the body text
  reaches the model.
- the signed lane stays unwrapped (no tool takes a private key), and there is still no
  credential of any kind;
- `TECHNOCORE_URL` / `TECHNOCORE_NICK` env config, the `WAIT_CEILING` clamp on
  `wait_for_message`, and the `technocore-mcp/{VERSION}` User-Agent.

### 2. The shim stays a shim, but the fetch becomes pluggable

Handlers remain "build URL → one GET → return body". But `urllib.request` only works on
CPython: Cloudflare Python Workers run on Pyodide, which has no raw sockets — outbound
HTTP there goes through the platform's JS `fetch` bridge. So isolate the single
fetch-and-decode call behind a small seam (an injectable async callable is enough; prefer
`async def` handlers throughout, which both the SDK and Workers want anyway):

- stdio / CPython path: stdlib only, as today (urllib in a thread or a stdlib-based async
  wrapper — do not add an HTTP client dependency just for this);
- Workers path: the platform fetch, wired up in the worker entry point.

While you are in that function, fix the trailing-`?` bug (#494): filter the `None` values
*before* deciding whether to append `?`, and add the regression test the issue describes
(capture the full URL, assert no bare `?` when all optional args are omitted).

### 3. Transports

- **stdio, unchanged from the outside**: the `technocore-mcp` console script and
  `uvx technocore-mcp` keep working exactly as documented in `mcp/README.md`.
- **streamable HTTP** for remote use. The tools are stateless — every call is one
  independent GET — so use the SDK's stateless mode; there is no session state worth
  keeping. SSE is deprecated; do not add it.

### 4. Cloudflare deployment scaffolding

Add, under `mcp/` (e.g. `mcp/worker/`), a Cloudflare Worker entry point and `wrangler`
config that serve the streamable-HTTP app, plus a short deploy doc. Ground yourself in the
**current** Cloudflare docs first — do not work from memory:

- <https://developers.cloudflare.com/agents/model-context-protocol/> (remote MCP servers,
  transport guidance — stateless streamable HTTP is the recommended shape; `McpAgent` is
  deprecated/feature-frozen, avoid it),
- Cloudflare's Python Workers docs and their post on Python MCP SDK / FastMCP support in
  Python Workers (<https://blog.cloudflare.com/streamable-http-mcp-servers-python/>).

The server is unauthenticated by design (the service it fronts is public and
world-writable; rate limiting is the origin's job), so no OAuth layer. Target shape:
`wrangler dev` serves the endpoint locally and an MCP client (the SDK's own client, or MCP
Inspector) completes initialize → tools/list → tools/call against it. Actually deploying
needs a Cloudflare account, so the deliverable is working local `wrangler dev` plus a
documented `wrangler deploy` path — do not block on the deploy itself.

**Decision point, not a silent fallback**: if the Python-on-Workers route turns out to be
materially broken for this shape (SDK not importable under Pyodide, package unsupported,
stateless streamable HTTP not achievable), stop and report what you found rather than
quietly porting the server to TypeScript. The one-implementation-in-Python property is a
requirement until the human says otherwise.

## Open issues this rewrite must resolve (verify each against the new code)

Validation/conformance reports against the hand-rolled protocol — most die with it, but
each needs to be *checked* against the SDK, not assumed:

- **#436** — requests without `jsonrpc: "2.0"` are accepted. SDK enforces the envelope;
  confirm and note it.
- **#105** — `tools/list` advertised an open schema while `tools/call` refused unknown
  arguments. With the SDK, the advertised schema and the enforcement come from the same
  pydantic model; check what the SDK emits for `additionalProperties` and that advertised
  and enforced agree in both directions.
- **#488** — the `Room` pattern `^[a-z0-9][a-z0-9_-]{0,47}$` was documentation, not
  validation. Declare it as a real constraint (`Field(pattern=...)`) so it lands in the
  advertised schema *and* is enforced before the network. Apply the same treatment to
  `nick` (same grammar) and look over the other documented bounds (`limit` 1–200, `seconds`
  0–10, `text` ≤ 4096) — advertise as JSON-Schema constraints whatever you also enforce,
  and nothing you don't.
- **#494** (dup #498, closed) — trailing-`?` URLs; fix in the fetch seam as above.
- **#206** — tool annotations. Implement the matrix from the issue via the SDK's
  `ToolAnnotations`: `read_room`, `wait_for_message`, `list_rooms`, `discover_rooms`,
  `read_note`, `list_notes`, `read_docs` read-only; `say` non-read-only, non-idempotent,
  additive; `write_note` non-read-only, potentially destructive; everything open-world.
- **#475** — `mcp/Dockerfile` installs from PyPI, so the image can lag the checkout.
  Rebuild the Dockerfile to install from the local checkout (`uv build` / `pip install ./mcp`).
- **#490** — a tracking issue whose "MCP schema" row is #488; the rest of it is
  service-side, out of scope here.

There are open PRs targeting several of these the rewrite supersedes: #437 (jsonrpc
validation), #495/#500/#508 (trailing `?`), #504 (Room pattern), #222 (annotations),
#478 (Dockerfile). Do not merge or rebase them; link them from your PR description as
superseded, and reference the issue numbers in commit messages so the fixes are findable.

## Dependency policy — the constraint that shaped the old design

- The **root** project's runtime dependencies do not change. The `mcp` SDK dependency
  lives in `mcp/pyproject.toml` only (pin a sensible floor on the current major).
- The root test suite imports `technocore_mcp` from `mcp/src` via `sys.path`, so running
  `tests/test_mcp.py` now needs the SDK importable in the dev env. Add `mcp` to the root
  `[dependency-groups] dev` (or a dedicated group CI syncs for that job) — dev-only, with
  a comment saying why it is there and that the service itself must never import it.
- `uvx technocore-mcp` now resolves real dependencies. Update every place that says "no
  dependencies / resolves nothing / starts immediately": `mcp/README.md`,
  `mcp/pyproject.toml` comments, and check `SKILL.md`, `glama.json`, `docs/`, and the root
  `README.md` for echoes of the claim.
- Keep the `mcp-name: io.github.flop-labs/technocore-chat` HTML comment in
  `mcp/README.md` — the MCP registry proves package ownership by finding it.

## Tests

- `tests/test_mcp.py` (641 lines) drives the old server as dict-in/dict-out via
  `server.handle(...)`, with `urlopen` redirected into a Starlette `TestClient` running the
  real app — so every test exercises JSON-RPC → URL construction → real handler → text
  rendering. **Keep that philosophy**: rewrite the harness to drive the SDK server through
  a real MCP client session over in-memory streams (the SDK ships utilities for exactly
  this), with the fetch seam pointed at the `TestClient`. Port the behavioral assertions
  (URL shapes, error-body passthrough, nick fallback, wait clamping, float-`since`
  handling, schema contents); drop only assertions about wire minutiae the SDK now owns,
  and say in the commit message which ones and why.
- `tests/test_mcp_falsey_params.py` guarded a hand-rolled framing bug (falsey `params`
  treated as missing). That is the SDK's jurisdiction now — replace it with an equivalent
  conformance check through the SDK if cheap, otherwise delete it with a justification.
- `tests/test_mcp_package.py` + `tests/verify_mcp_dist.py` (license files inside wheel and
  sdist) must still pass; CI runs `uv build --project mcp` then `verify_mcp_dist.py`.
- New coverage worth having: the streamable-HTTP app answers initialize/tools/list/
  tools/call (SDK client against the ASGI app in-process); the annotations matrix; the
  pattern/bounds constraints appearing in the advertised schema; #494's URL regression.

## Definition of done

1. `uv sync --frozen && uv run ruff check . && uv run ruff format --check . && uv run ty
   check && uv run coverage run -m pytest tests -q` — all green, plus whatever env the mcp
   tests need.
2. stdio smoke: `uvx --from ./mcp technocore-mcp` (or the built wheel) completes an
   initialize → tools/list → tools/call round trip with a real MCP client.
3. `wrangler dev` serves the streamable-HTTP endpoint locally and the same round trip
   passes against it — or a written report of the concrete blocker (see the decision
   point above).
4. Docs updated: `mcp/README.md` (install, the reversed "why not the SDK" story, the
   remote endpoint and how to point a client at it, Docker), `mcp/server.json` (add the
   `remotes` streamable-http entry only if a real URL exists — otherwise document the
   post-deploy step), `CHANGELOG.md`.
5. `.github/workflows/publish-mcp.yml`'s version-lockstep check still passes; `VERSION`
   in `server.py` stays the single source the wheel derives from.
6. Issues #436, #105, #488, #494, #206, #475 each verifiably addressed (test or noted
   SDK behavior), referenced in commit messages; superseded PRs listed in the PR
   description.

Work on a feature branch, commit in reviewable slices (SDK rewrite; fetch seam + #494;
annotations; Dockerfile; worker scaffolding; docs), and do not touch the service under
`src/` except where the test harness requires.
