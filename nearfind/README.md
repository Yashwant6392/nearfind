# NearFind

NearFind is a hyperlocal product-finding MVP. A seeker posts an urgent product request with a reference image, nearby providers discover it on a Leaflet/OpenStreetMap map, respond with real product availability, and the seeker compares images, selects a provider, contacts them and resolves the request.

flask --app app run

- Supabase Auth signup/login for seekers and providers.
- Server-side Flask sessions and role-gated dashboards.

## Production startup

- Seeker product requests with image upload, urgency, radius and expiration.
- Provider demand map with Haversine distance filtering.
- Provider response form with price, message and product image upload.
- Live seeker response page polling `/query/responses/<query_id>` every 3 seconds.
- Green provider markers, yellow pulsing request markers and blue user markers.
- Side-by-side reference/product image comparison.
- Provider selection state machine: `open -> matched`, responses `available -> selected/rejected`.
- Resolve state transition: `matched -> resolved`.
- Call, WhatsApp and OpenStreetMap directions actions.

## Architecture

Flask serves all pages and APIs from one process. Supabase handles Auth, PostgreSQL and Storage. The browser uses vanilla JavaScript, Leaflet 1.9.4 and OpenStreetMap tiles. No service-role key is sent to the browser; all privileged Supabase work happens server-side.

The application uses Flask sessions for identity, server-generated CSRF tokens for state-changing requests, role decorators for seeker/provider authorization, server-side Haversine distance checks for provider eligibility, and the `public.select_provider_for_query` PostgreSQL function for atomic provider selection. Query lifecycle transitions are `open -> matched -> resolved` or `open -> expired`.

## Project Structure

```text
nearfind/
  app.py
  config.py
  schema.sql
  requirements.txt
  .env.example
  services/
  templates/
  static/
```

## Setup on Windows

Use Python 3.11 or newer.

```powershell
cd "D:\projects\near find codex\nearfind"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Fill `.env`:

```env
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
SECRET_KEY=change-this-long-random-string
FLASK_ENV=development
```

Run:

```powershell
flask --app app run
```

Open `http://127.0.0.1:5000`.

## Production startup

Install dependencies from `requirements.txt`, configure all required environment variables, and run behind a production WSGI server. For example with Waitress:

```powershell
waitress-serve --listen=0.0.0.0:8000 app:app
```

Set `FLASK_ENV=production` and use a long random `SECRET_KEY`. Do not run Flask's development server or debugger in production. Configure HTTPS at the hosting or reverse-proxy layer, and set the Supabase URL and matching public/service credentials through the deployment environment.

The application returns structured JSON errors for API requests (`success: false` with an error `code` and `message`) and safe HTML error pages for normal browser requests. Unexpected failures are logged server-side with route and user context but technical details, credentials, and stack traces are never sent to the browser. Browser API calls use bounded timeouts and recoverable polling; an expired session redirects to `/login` with a session-expiry message.

## Supabase Setup

1. Create a Supabase project.
2. In Authentication settings, disable email confirmation for this MVP.
3. Run [schema.sql](schema.sql) in the Supabase SQL editor.
4. Run [migrations/001_notifications.sql](migrations/001_notifications.sql) after the base schema.
5. Create two public storage buckets:
   - `query-images`
   - `response-images`
6. Add storage policies allowing public reads for both buckets.
7. Add authenticated insert policies for both buckets, or rely on the Flask server using the service-role key.
8. Run [migrations/003_live_location.sql](migrations/003_live_location.sql) after the chat migration.

Database migrations are manual and ordered: base schema, notifications, chat, then live location. The application never executes migrations automatically.

For production Auth, configure custom SMTP, choose an explicit email-confirmation policy, and set Supabase Auth rate limits appropriate to expected traffic. The application does not bypass hosted Auth rate limits or create users through the Admin API.

Example public-read policy:

```sql
create policy "Public read query images"
on storage.objects for select
using (bucket_id = 'query-images');

create policy "Public read response images"
on storage.objects for select
using (bucket_id = 'response-images');
```

## Database Notes

The app uses these tables:

- `users`: profile records linked to Supabase Auth users.
- `queries`: seeker requests with statuses `open`, `matched`, `resolved`, `closed`, `expired`.
- `responses`: provider responses with statuses `available`, `selected`, `rejected`, `sold`.
- `notifications`: user-scoped lifecycle notifications. Apply [migrations/001_notifications.sql](migrations/001_notifications.sql) after the base schema.
- `conversations` and `messages`: selected-provider chat. Apply [migrations/002_chat.sql](migrations/002_chat.sql) after the notifications migration.

A partial unique index prevents duplicate active provider responses for the same query.

Authenticated notification APIs are `GET /notifications` and `POST /notifications/<notification_id>/read`. Notification IDs are deterministic for recipient/event/query/response, and the database primary key prevents duplicate lifecycle events.

Chat APIs are `GET /chat/<conversation_id>/messages`, `POST /chat/<conversation_id>/messages`, and `POST /chat/<conversation_id>/read`. Apply `migrations/002_chat.sql` manually in Supabase; the application does not execute migrations automatically.

## Testing the Demo Flow

1. Sign up as a seeker and allow location access, or enter latitude/longitude manually.
2. Post `Dell 65W USB-C Charger`, upload a reference image, set radius to `5 km`, and click `Find It`.
3. In another browser/session, sign up as a provider near the same coordinates.
4. Open the provider dashboard. The seeker request appears as a yellow pulsing marker and card.
5. Click `I Have It`, upload the actual product image, enter price and message, then submit.
6. Return to the seeker response page. Within 3 seconds, the provider card and green marker appear.
7. Compare images, choose the provider, then use Call, WhatsApp or Get Directions.
8. Mark the request resolved after completion.

## Location and Distance

Dashboards try `navigator.geolocation.getCurrentPosition()`. If permission is denied, users can enter coordinates manually. Flask stores refreshed coordinates in the `users` table and session. Nearby discovery uses the Haversine formula in `services/location_service.py`, with distances calculated server-side.

## Security Considerations

- All dashboard and API routes are role-protected.
- Seekers cannot respond as providers.
- Providers cannot create seeker requests.
- `/seeker/responses/<query_id>` and response APIs verify query ownership.
- Uploads validate MIME type, file signature and size.
- The service-role key stays only in `.env` and server code.
- Session cookies are HTTP-only and SameSite=Lax.
- Sessions are permanent with an eight-hour lifetime and are cleared before establishing a new login identity.
- State-changing routes require a server-generated CSRF token.
- Coordinates must be finite and within valid latitude/longitude bounds; radius and text inputs are bounded server-side.
- Uploads require a matching JPG, PNG, or WEBP extension, MIME type, file signature, and a maximum size of 5 MB. Storage paths use UUIDs and sanitized filenames are never used as object paths.
- Rejected provider responses retain only historical display fields; phone numbers and exact coordinates are withheld.
- Flask currently uses service-role database access for server-side table operations. Supabase RLS remains enabled as a protection layer for non-service-role access, but Flask authorization checks are the primary enforcement layer for application requests.
- Provider selection runs through the `select_provider_for_query` PostgreSQL function in `schema.sql`. The function locks the query, rejects expired or invalid transitions, validates the response relationship/status, and performs the matched/rejected transition atomically.
- If Auth signup succeeds but profile creation fails, the server clears the Flask session, logs the technical failure, and shows a safe error. Auth rollback is not attempted through browser authentication APIs.

## Known Limitations

- Replace polling with Supabase Realtime.
- Add provider opening hours and inventory categories.
- Add approximate-location controls per individual provider.
- The test suite uses mocked Supabase clients and does not perform live destructive database or storage operations.
- Flask authorization and ownership checks are critical because normal server-side table operations use the service-role credential and therefore bypass Supabase RLS.

## Tests

From the workspace root:

```powershell
pytest -q
```
