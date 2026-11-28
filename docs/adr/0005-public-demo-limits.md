# ADR 0005: Public demo limits (ask-only key, per-IP rate limit, hard daily spend cap)

**Status:** accepted (P5B, 2026-11-28)

## Context

The public demo at pagecite.duckdns.org lets anyone ask questions about the 60 RBI circulars. Each question costs real OpenAI money: about $0.004 for hybrid + rerank + answer, and up to about $0.01 in the worst case. The site must stay useful to visitors without letting anyone run up the bill, write data, or exhaust a 1-worker, 8-thread API.

## Decision

1. **An ask-only key** (`api_keys`, scope `ask`, prefix `pca_`, V5). Authorization is **deny by default**: such a key may use GET/HEAD and `POST /ask` only, and every other route returns 403 `FORBIDDEN_FOR_KEY`. That covers upload, complete, replay and cancel, and any route added later without touching auth.
   - Caddy serves the key at runtime from the server's `.env` (`/config.json`), so it isn't in the bundle, the images or GitHub.
   - CI fails if the built bundle contains any `pc_` or `pca_` key.
2. **A per-IP rate limit** on `/ask`: 10 per minute and 20 per UTC day, as fixed windows in Redis (one MULTI pipeline of INCR + EXPIRE).
   - **Every attempt counts,** before the body is validated, so invalid requests can't probe for free.
   - **IPv6 is bucketed by /64.**
   - **Client IP:** the one Caddy appends to X-Forwarded-For (`ProxyFix(x_for=1)`). Only Caddy can reach the API.
3. **A global daily spend cap** ($0.50/UTC day) that is **hard even under concurrency**:
   - **Reserve:** before any model call, a Lua script reserves `SPEND_RESERVE_USD` (the worst-case cost of one request). It refuses with 429 `DEMO_LIMIT_REACHED` and a friendly message when `spent + reserve > cap`.
   - **Settle:** when the request ends (the stream's `finally`), the reservation is replaced by the cost actually recorded per model call (`apps/api/retrieval/pricing.py`).
   - **Unknown cost:** if the cost can't be known (the client left before the answer's usage arrived, or a call has no price), the reservation stands.
   - **Unpriced models:** when a cap is set, the API refuses to start if a configured model has no price.
4. **Fail closed:** if the limits' Redis doesn't answer within 1 s, `/ask` returns 503 `LIMITS_UNAVAILABLE`, never unmetered service.
5. **Question length** is capped at 500 characters (422 `QUESTION_TOO_LONG`) for every tenant.
6. **Storage:** the limits live in Redis DB 2, apart from the index (DB 0) and the broker (DB 1), with key prefix `pagecite:` and TTLs on every key.

## Consequences

- **Overshoot:** with reserve-then-settle, at most one request's real cost minus its reservation can go past the cap, and that's when the reserve is set too low for a request. At 20 asks a day, one IP can spend about $0.08, so reaching the daily cap takes about 6 or more distinct IPs (or /64s).
- **Shared NAT:** visitors behind one NAT or one IPv6 /64 share a limit. That's accepted for a demo.
- **Backstops:** the OpenAI project's monthly budget, and the AWS budget alert.
- **Redis 8 gotcha** (found while testing this ADR): `FLUSHDB` on *any* database drops every search index, not only that database's keys. Separate DBs therefore don't protect the index from a flush.
  - Nothing may FLUSHDB except the P2 reindex check; a unit test greps for it.
  - Limits are reset by deleting `pagecite:*` keys (docs/runbook.md).
- **The admin key:** the demo tenant's full key exists only in the owner's password manager and is used only for ingestion (`scripts/prod_ingest.py`).
