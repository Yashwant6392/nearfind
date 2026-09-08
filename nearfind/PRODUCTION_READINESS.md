# NearFind Production Readiness Checklist

This checklist records local code review results for the Flask, Supabase, Jinja, and vanilla JavaScript application. Live Supabase/Auth, Storage policy, HTTPS, and deployment checks require the target environment.

## Core Workflows

- [x] Seeker query creation validates text, coordinates, radius, urgency, expiry, and ownership.
- [x] Seeker dashboard counts responses by exact query ID.
- [x] Provider discovery checks provider role, active status, coordinates, open/non-expired queries, and server-side Haversine distance.
- [x] Provider response uses the authenticated session provider ID.
- [x] Seeker response API checks query ownership and uses `users!responses_provider_id_fkey(...)`.
- [x] Provider selection delegates the atomic lifecycle transition to `select_provider_for_query`.
- [x] Query resolution now updates only an owned `matched` query.
- [x] Expiry now updates only rows still in `open` state.
- [ ] Complete real seeker/provider/browser E2E with eligible accounts.

## Security

- [x] State-changing routes require CSRF tokens.
- [x] Role decorators protect seeker/provider routes.
- [x] Query ownership is checked server-side.
- [x] Provider IDs are taken from the Flask session.
- [x] User content is rendered with textContent or Jinja escaping.
- [x] Uploads validate size, MIME, extension, and file signature.
- [x] Response edits no longer trust a client-supplied existing image URL.
- [x] Session cookies are HTTP-only and SameSite Lax.
- [ ] Add application-level rate limiting for Auth, uploads, APIs, and polling.
- [ ] Revalidate Supabase Auth/session state during long-lived Flask sessions.
- [ ] Reduce service-role database access or formally harden and test the server authorization boundary.

## Database and RLS

- [x] Foreign keys and lifecycle constraints are defined in `schema.sql`.
- [x] RLS is enabled on users, queries, and responses.
- [x] Selection RPC locks and validates query/response state.
- [ ] Verify production RLS and Storage policies directly in the target Supabase project.
- [x] Handle concurrent provider response submissions through the existing unique index and conflict recovery.

## Storage

- [x] Query and response uploads use separate buckets.
- [x] Object paths use UUIDs and safe extensions.
- [x] Browser code never receives the service-role key.
- [ ] Verify bucket privacy, upload, read, and cleanup policies in production.
- [ ] Add ownership-bound object lifecycle and orphan cleanup if private storage is required.

## Frontend

- [x] Seeker polling prevents overlapping requests and stops on terminal state/page unload.
- [x] Provider discovery waits for geolocation refresh and prevents overlapping fetches.
- [x] Response empty, loading, timeout, malformed JSON, and network-error states are handled.
- [x] Dynamic user data is inserted without unsafe innerHTML.
- [ ] Perform authenticated desktop, tablet, mobile, keyboard, and screen-reader verification.

## Authentication and Configuration

- [x] Signup/login use public Supabase Auth APIs and handle Auth rate-limit responses.
- [x] Duplicate form submission is disabled in the browser.
- [x] `.env` is ignored and `.env.example` contains placeholders only.
- [x] Flask debug mode follows `FLASK_ENV`.
- [x] Production startup fails fast when required Supabase settings are missing or `SECRET_KEY` is shorter than 32 characters.
- [x] `/healthz` provides a deployment readiness probe without exposing configuration values.
- [x] Waitress production startup is documented and pinned.
- [ ] Configure production SMTP, email-confirmation policy, Auth rate limits, HTTPS, and a long random `SECRET_KEY`.
- [ ] Verify deployment environment variables without exposing them.

## Validation

- [x] `python -m pytest -q` passes.
- [x] `python -m compileall -q .` passes.
- [x] `node --check static/js/main.js` passes.
- [x] `node --check static/js/provider-map.js` passes.
- [x] `node --check static/js/seeker-map.js` passes.
- [x] `git diff --check` passes.
- [x] Local `/healthz` readiness probe returns `200` when the configured environment is complete.
- [ ] Run authenticated live E2E against the deployment target.

## Deployment Blockers

- A real provider account/session and an eligible provider location are required to complete live E2E.
- Supabase Auth, SMTP, RLS, Storage policies, HTTPS, and service-role boundary require target-environment verification.
- The current application uses server-side service-role access for table operations; Flask authorization checks remain critical until that boundary is redesigned.
