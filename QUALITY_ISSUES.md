# Code Quality Issues

**Analysis Date:** May 15, 2026  
**Status:** Identified, awaiting refactoring plan

---

## Critical Issues

### 1. Data Access Layer Fragmentation
**Severity:** CRITICAL  
**Category:** Architecture

Three conflicting database access strategies are used simultaneously:
- Raw `psycopg2` connection pooling with SQL strings
- SQLAlchemy ORM with models/sessions  
- Direct raw SQL queries embedded throughout the codebase

**Consequences:**
- DRY violations: duplicate connection pooling logic
- No unified query interface or error handling across approaches
- Large portions of code bypass migrations (raw SQL only, no Alembic coverage)
- Inconsistent patterns make the codebase hard to maintain

**Affected Files:**
- `src/ump/api/db_handler.py` (dual pool implementation)
- `src/ump/api/jobs.py` (raw SQL)
- `src/ump/api/models/job.py` (raw SQL inserts)
- `src/ump/api/routes/jobs.py` (ORM)

---

### 2. SQL Injection Vulnerabilities
**Severity:** CRITICAL  
**Category:** Security

SQL conditions constructed via direct string interpolation:

```python
# From src/ump/api/jobs.py
conditions.append(f"j.user_id = '{user}' or u.user_id = '{user}'")
```

**Consequences:**
- Production-critical vulnerability
- Inconsistent security posture: parameterized queries used in some places, unsafe strings in others
- No linting or static analysis preventing this pattern

**Affected Files:**
- `src/ump/api/jobs.py` (user_id interpolation)

---

### 3. Massive SRP Violations Across Layers
**Severity:** HIGH  
**Category:** Architecture

Each component handles too many responsibilities:

**`src/ump/main.py`** (explicitly flagged in code):
- App factory for migrations
- Logging setup
- Scheduled cleanup runner
- Flask app initialization
- Token verification

**`src/ump/api/models/job.py`** (per TODO comments):
- Data access logic
- Business logic
- Metadata handling
- All mixed in one class

**Routes** (e.g., `src/ump/api/routes/jobs.py`):
- Complex query logic instead of delegating to services
- Direct database access instead of using repositories

**Consequences:**
- Hard to test individual concerns
- Changes propagate unexpectedly
- Code reuse impossible across concerns
- Difficult reasoning about data flow

---

### 4. Authentication & JWT Handling Is Brittle
**Severity:** HIGH  
**Category:** Security/Architecture

**Issues:**
- Manual JWT parsing with direct dict access instead of proper JWT libraries
- TODO comment: "manually parsing jwt is not recommended, use a library like PyJWT or better Authlib"
- Error handling and token verification spread across multiple files
- Comments in code: "this is NOT good for production environments!"

**Affected Files:**
- `src/ump/api/processes.py` (lines 20-27)
- `src/ump/main.py` (token verification)

**Consequences:**
- Susceptible to JWT manipulation or bypass
- No centralized auth policy enforcement
- Inconsistent error responses

---

### 5. Async/Sync Boundary Is Poorly Defined
**Severity:** HIGH  
**Category:** Performance/Concurrency

**Issues:**
- Routes call `asyncio.run()` to execute async code synchronously
- This blocks the request handler in a multi-worker environment
- Async patterns used inconsistently (some routes async, some sync)
- No clear ownership of when to use async vs. sync

**Affected Files:**
- `src/ump/api/routes/jobs.py` (line 27: `asyncio.run(job.results())`)
- `src/ump/api/processes.py` (async but called from sync routes)

**Consequences:**
- Deadlocks or race conditions under load
- Poor request throughput
- Thread safety violations in multi-worker deployments

---

### 6. No Input Validation Framework
**Severity:** HIGH  
**Category:** Data Quality/Reliability

**Issues:**
- Processes.py manually checks for dict keys instead of validating with Pydantic
- Routes accept request data without schema validation
- TODO: "instead of manually checking for a key, we should validate the response using a pydantic model or json schema"
- No consistent validation at API boundaries

**Affected Files:**
- `src/ump/api/processes.py` (line 101)
- `src/ump/api/routes/jobs.py` (all route handlers)

**Consequences:**
- Invalid data propagates through the system
- Hard to debug where data went wrong
- API contracts not enforced

---

### 7. Scattered Configuration & Hard-coded Values
**Severity:** MEDIUM  
**Category:** Maintainability

**Issues:**
- Timeouts, table names, URLs, magic numbers spread throughout code
- Multiple database connection configurations without abstraction
- Hard-coded values make testing and deployment inflexible

**Examples:**
- `ClientTimeout` defined inline in multiple files
- `RESULTS_TABLE_NAME` referenced in `src/ump/geoserver/geoserver.py`
- Multiple connection pool definitions (psycopg2 + SQLAlchemy)

**Affected Files:**
- `src/ump/api/models/job.py` (results_client_timeout)
- `src/ump/api/processes.py` (client_timeout)
- `src/ump/api/db_handler.py` (dual connection setups)

---

### 8. Error Handling Is Inconsistent & Often Silent
**Severity:** HIGH  
**Category:** Observability/Reliability

**Issues:**
- Broad `except Exception` blocks swallow meaningful error context
- Logging vs. raising exceptions used inconsistently
- Some failures silently skip work (e.g., geoserver cleanup failures)
- No structured error recovery paths

**Examples from code:**
```python
# Catches all, logs, and continues - real error hidden
except Exception as e:
    logging.error("Failed to cleanup geoserver results for job %s: %s", job_id, e)
```

**Affected Files:**
- `src/ump/main.py` (cleanup function)
- `src/ump/api/processes.py` (error handling in fetch_provider_processes)

**Consequences:**
- Production failures masked as warnings
- Debugging becomes a guessing game
- No rollback or recovery mechanisms

---

### 9. Schema Normalization Missing
**Severity:** MEDIUM  
**Category:** Data Quality

**Issues:**
- Raw SQL inserts with redundant fields
- No ORM models for core tables (Job, processes)
- Database schema not enforced at application layer
- Alembic migrations exist but are bypassed by raw SQL

**Affected Files:**
- `src/ump/api/models/job.py` (raw INSERT queries)
- `migrations/` (schema enforcement gaps)

**Consequences:**
- Data inconsistencies over time
- Schema drift between code and database
- No validation of field constraints at application level

---

### 10. Resource Lifecycle Management Is Unclear
**Severity:** MEDIUM  
**Category:** Reliability

**Issues:**
- Two separate connection pool implementations (psycopg2 + SQLAlchemy)
- Unclear when connections are released vs. leaked
- Global state (`PROVIDERS`, `RELOAD_TIMER`) without proper synchronization everywhere

**Affected Files:**
- `src/ump/api/db_handler.py` (dual pools)
- `src/ump/api/providers.py` (global state with locks, but not everywhere)
- `src/ump/main.py` (atexit handler)

**Consequences:**
- Connection exhaustion under sustained load
- Memory leaks if cleanup fails
- Race conditions in multi-threaded scenarios

---

## Summary by Severity

| Count | Severity | Issues |
|-------|----------|--------|
| 2 | CRITICAL | Data fragmentation, SQL injection |
| 5 | HIGH | SRP, auth, async/sync, validation, error handling |
| 3 | MEDIUM | Configuration, schema, resource lifecycle |

**Total Issues:** 10 major categories affecting production reliability and maintainability.

---

## Notes for Refactoring

- All issues are documented in code as TODOs (34 matches found)
- No single issue is isolated; most are interconnected
- A phased refactoring strategy is required to address dependencies
- Security issues (SQL injection, JWT handling) should be addressed in Phase 1
