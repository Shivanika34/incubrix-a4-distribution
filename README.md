# incubrix-a4-distribution
Connector service for publishing content to forums and open networks, with a posting-rules engine and certification harness

## Publishing jobs

Submit one content item to one or more configured destinations. The request is
asynchronous and requires an idempotency key:

```http
POST /api/v1/publishing-jobs
Idempotency-Key: article-2026-10-17
Content-Type: application/json
```

```json
{
  "content_item": {
    "title": "A useful article",
    "body": "Markdown content",
    "canonical_url": "https://example.test/article"
  },
  "destination_ids": ["destination-1", "destination-2"],
  "connections": {
    "destination-2": {"api_key": "connection-secret"}
  }
}
```

The endpoint returns `202` with a `job_id`. Poll
`GET /api/v1/jobs/{job_id}` for progress. The terminal response includes one
destination outcome per requested ID, including `status`, `error_code`,
`reason`, `platform_post_id`, `live_url`, and (for the assisted connector) a
`posting_pack`. Reusing the same idempotency key and request returns the
original job; reusing it with a different request returns
`IDEMPOTENCY_KEY_REUSED`.

The Core connector registry includes configuration-driven Discourse, Lemmy,
Dev.to, Ghost, signed webhook, and assisted connectors. In simulation mode,
the native connectors use the local mock platform and do not contact external
services. Real connector instances are selected by a destination row and
receive their non-secret connection settings in the request.

For local certification, Compose also starts stateful, disposable Lemmy and
Ghost-compatible test services on `localhost:8081` and `localhost:8082`.
They implement only the API calls needed by the connector contract and are
not real community or publishing products. Pass `local_test: true` with the
service `base_url` to opt into these services while simulation mode remains
enabled. A Discourse-compatible service can use the same local platform
adapter; a dedicated Discourse container is intentionally deferred until an
approved image and fixture are selected.

## Destination certification

Run the certification harness with `POST /api/v1/certifications` and an
`Idempotency-Key`. The background job publishes a temporary test post through
the configured connector, confirms its live status, deletes it, and stores the
evidence in `certification_runs`. Successful native runs update
`last_certified_at` and promote an L0/L2 destination to L3; assisted
destinations produce a posting pack and are promoted to L1 without pretending
that a manual post was published. Each completed run also writes a redacted
JSON evidence file under
`artifacts/certification/{connector}/{certification_run_id}.json`. These
local test artifacts contain timestamps, result IDs/URLs, status, deletion
confirmation, and failure details, but never connection credentials.

```json
{
  "destination_id": "destination-1",
  "connection": {
    "api_key": "connection-secret"
  }
}
```
