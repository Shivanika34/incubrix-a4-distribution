# IncuBrix Assessment 4 project rules

Re-read the relevant section of `docs/brief.pdf` before each task.

1. Implement only what the brief asks, in this order: Must-haves 1–9, Core, then the 10-point Integration Standard. Do not build Advanced or Outstanding items unless requested.
2. Use Python 3.11+, FastAPI, Postgres, MinIO, Docker Compose, and pytest.
3. Load paths, keys, and settings from environment variables or a config file; document them in `.env.example`.
4. Never put secrets in logs, tests, or the repository.
5. Put routes under `/api/v1/`, except `/connector/v1/{connector_key}/{method}`, `/health`, and `/ready`.
6. Return errors as `{"error": {"code", "message", "retryable", "details"}}` with UPPER_SNAKE_CASE codes.
7. Put every external call behind an adapter with a mock implementation. Tests must run without keys or network access.
8. Write a test for every behavior before or with the implementation. Keep code small, simple, commented, and explainable.
9. After each task, run all tests, list changed files, and summarize each in three lines.
10. Do not refactor or touch files outside the current task.

