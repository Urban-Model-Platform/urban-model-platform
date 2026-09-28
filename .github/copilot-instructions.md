# Coding Agent Principles

## 1. Think Before Coding

Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

Minimum code that solves the problem. Nothing speculative.

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

Touch only what you must. Clean up only your own mess.

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

Every changed line should trace directly to the user's request.

## 4. Pythonic Code

Prefer standard-library patterns over hand-rolled equivalents.

- **`itertools` / `functools`**: use `chain`, `islice`, `groupby`, `wraps`,
  `partial`, `reduce` etc. — don't reimplement them.
- **`contextlib`**: use `AsyncExitStack` / `ExitStack` when managing multiple
  context managers dynamically; use `contextmanager` / `asynccontextmanager`
  for generator-based contexts.
- **`asyncio`**: prefer `asyncio.TaskGroup` (Python 3.11+) over bare
  `gather`+`create_task` where error propagation matters; use `asyncio.Queue`
  for producer/consumer pipelines instead of callbacks.
- **`dataclasses` / `typing`**: use `@dataclass`, `NamedTuple`, `TypedDict`,
  `Protocol` before writing boilerplate classes.
- **Iteration**: prefer comprehensions and generators over explicit `for`+`append`.
- **No reinventing the wheel**: if the standard library or an already-declared
  dependency solves it cleanly, use it. Don't add new dependencies for things
  the stdlib covers.

## 5. Goal-Driven Execution

Define success criteria. Loop until verified.

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan before starting:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

## 6. Unit and Integration Tests

The acceptance criteria in the ticket description are the source of truth: each
criterion must map 1:1 to at least one test, listed explicitly so a reviewer
can audit coverage in one read. So interview the user about the acceptance criteria before implementing. The final commit must run `make test` (i.e.
`poetry run pytest`) green — the same command a reviewer runs locally.

- **Meaningful unit tests are present** for every new or changed code path.
  "Meaningful" means:
  - Tests assert behaviour, not implementation details.
  - Happy path, error paths, and boundary values are covered.
  - Tests fail without the change and pass with it.
- **Tests map 1:1 to acceptance criteria** — the MR description lists which
  test(s) verify which criterion, so a reviewer can audit coverage in one
  read.
- **Test data and secrets are handled correctly** — no real PII, no real
  credentials, no network calls to production. Mocks, fakes, and fixtures
  are described in the test or its `conftest.py`.
- **`make test` (or `poetry run pytest`) is green** on the final commit,
  with the same command a reviewer would run locally.

### 6.1 Unit Tests

Unit tests exercise a single unit of behaviour in isolation. They must be
fast, deterministic, and free of infrastructure dependencies (no DB, no
network, no filesystem outside `tmp_path`, no real clock, no real secrets).

- **Framework and structure.** Use **pytest**. Organise related tests in
  **`Test*`-prefixed classes** that group a coherent set of cases for one
  unit; keep standalone test functions for trivial or one-off cases. Do
  not use `unittest.TestCase` subclasses unless you have a specific
  reason.
- **Naming.** Test files: `test_*.py`. Test classes:
  `Test{UnitUnderTest}` (e.g. `TestInvoiceCalculator`). Test methods:
  `test_{behaviour_under_condition}` in plain prose, describing the
  *behaviour*, not the implementation — e.g.
  `test_rejects_negative_quantity` rather than
  `test_raises_value_error_in_add`. Names should read like a spec.
- **AAA, symmetrically.** Structure every test with three phases:
  **Arrange** (set up inputs, collaborators, state), **Act** (a single
  observable action — the *one* behaviour under test), and **Assert**
  (the observable outcomes). Keep the phases visually separated (blank
  lines or comments). Symmetric means: don't let `Arrange` or `Assert`
  dominate the body; if setup or assertions dwarf the act, extract a
  helper or split the test.
- **One act per test.** A test must perform exactly **one** Act. Multiple
  acts in one test mean a failure can't be localised and the test is
  really several tests glued together — split it. Use parametrisation to
  vary inputs around one behaviour, not to multiply acts.
- **Minimally passing tests.** A test should fail when the production
  code is broken and pass when it is correct — nothing more. No
  tautologies, no re-asserting language semantics, no asserting on
  internal state that the public contract doesn't promise. If the
  assertion doesn't help a future reader diagnose a regression, delete
  it.
- **No production logic in tests.** Tests must not contain business
  calculations, branching that mirrors the code under test, or
  "expected" values computed by re-implementing the SUT. Compute expected
  values directly from the spec or a trusted source; if you can't, the
  test probably belongs at a different layer.
- **Clean code applies to tests too.** Tests are production code: small
  functions, clear names, no dead helpers, no copy-pasted setup that
  diverges silently. Helpers go in `conftest.py` or `tests/helpers/`
  when reused; otherwise inline them. DRY is a *tool*; readability is
  the goal.
- **Mocks, fakes, fixtures.** Mock only at the **boundary** the unit
  crosses — the collaborator it talks to — never the unit under test
  itself, and never the language/library it uses. Prefer real fakes
  (in-memory implementations of an interface) over `MagicMock` when the
  fake is short and stable; reserve `unittest.mock` for narrow,
  well-justified seams. **Do not mock away everything.** A test where
  every collaborator is mocked is testing the mocks, not the code.
  Mocks, fakes, and fixtures must be described in the test or in
  `conftest.py` so a reader knows what stands in for what.
- **Coverage of cases.** For every new or changed code path:
  - Happy path (typical valid input).
  - Error / failure paths (each distinct exception or return value the
    contract promises).
  - Boundary values (empty, zero, one, max, off-by-one, type boundaries).
  Use `@pytest.mark.parametrize` to express them as data, not as
  copy-pasted methods.
- **Secrets and data.** No real PII, no real credentials, no live
  network calls to production or any external service. Generate test
  data with factories or `factory_boy`-style helpers; load fixtures from
  `tests/fixtures/` checked into the repo.
- **Meaningful tests beat coverage numbers.** When there is no core
  logic to test (e.g. a thin shim, a config class, a re-export), do not
  invent tests just to bump coverage. A small set of meaningful
  assertions that pin down the contract is worth more than 100% line
  coverage of code that has nothing to assert. Add tests only when they
  would catch a realistic regression.

### 6.2 Integration Tests

Integration tests verify that independently developed units work together
correctly when connected — across module boundaries, with a real database,
an HTTP API, a message bus, the filesystem, or another process. They
complement unit tests; they do not replace them. Always ask before implementing
integration tests.

- **Test the seam, not the whole system.** Prefer **narrow** integration
  tests that exercise the code on *one* side of a boundary against a
  real or faithful stand-in for the *other* side. A test that boots the
  entire system for every case is slow, flaky, and asserts on too much
  at once; split it.
- **Use real infrastructure where the boundary *is* the point.** When
  integration value comes from talking to a real Postgres, a real Redis,
  or a real HTTP service, run that infrastructure in Docker (compose)
  and hit it for real. The behaviour of a SQL query against SQLite is
  not the behaviour of the same query against Postgres. State explicitly
  which services the suite needs in its docstring or `conftest.py`.
- **Mark and gate them.** Tag integration tests with
  `@pytest.mark.integration` and keep the default `make test` run fast
  enough for everyday feedback (typically unit tests only). Provide an
  explicit target for the full suite, e.g. `make test-integration`,
  that brings up dependencies and runs the marked tests. CI runs both.
- **Determinism.** Integration tests must be deterministic. Quarantine
  and fix flakiness at the root cause; do not paper over it with
  `time.sleep`, retry loops, or `--flake-reruns`. Concrete tactics:
  - **Time:** inject a clock; never call `datetime.now()` directly in
    code that tests need to control. Where wall-clock matters (TTL,
    scheduled jobs), use `freezegun` or a fakeable `Clock` injected via
    DI.
  - **Async / queues:** poll with a bounded timeout and a fast-failing
    assertion, never unbounded waits.
  - **Remote services:** when a real service is unavailable, use a
    contract-verified test double (in-process fake or contract test),
    not the live service.
  - **Resource leaks:** each test owns the resources it creates; clean
    up in fixture finalisers so the next test starts from a known state.
- **Isolation per test.** Each integration test starts from a known
  state: a per-test schema, a per-test database, a per-test queue
  namespace, a transaction-rollback wrapper, or a freshly seeded
  container. Tests must not depend on order or on state left by another
  test.
- **Assert on observable behaviour, not internals.** Assert on HTTP
  status codes, response bodies, persisted rows observed via the public
  API, emitted events. Don't assert on log strings, internal call
  counts, or private attributes — these couple the test to the
  implementation and produce brittle failures.
- **Contract over end-to-end.** When two services exchange a contract,
  encode it as a contract test (provider + consumer) so each side can
  evolve without a heavy end-to-end suite. Reserve full end-to-end
  tests for a small set of critical smoke paths.
- **Test data and secrets.** Same rules as unit tests, plus: load
  realistic-but-synthetic seed data from versioned fixtures; never
  snapshot production; redact anything that looks like PII in fixtures
  even if synthetic.
- **Clean code applies.** Integration tests are production code. Long
  procedural scripts that mutate global state are a maintenance burden
  — refactor setup into fixtures, helpers into `conftest.py`, and keep
  each test focused on one observable outcome.

### 7. graphify

For any question about this repo's architecture, structure, components, or how to add/modify/find
code, your **first tool call must be** to read `graphify-out/GRAPH_REPORT.md` (if it exists).

Triggers: "how do I…", "where is…", "what does … do", "add/modify a <component>",
"explain the architecture", or anything that depends on how files or classes relate.

After reading the report (and `graphify-out/wiki/index.md` for deep questions), answer from the
graph. Only read source files when (a) modifying/debugging specific code, (b) the graph lacks
the needed detail, or (c) the graph is missing or stale.

Type `/graphify` in Copilot Chat to build or update the graph.

### 8. Hexagonal Architecture

For any code generate, use hexagonal architecture and domain driven 
design. 

