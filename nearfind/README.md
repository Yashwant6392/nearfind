# NearFind

NearFind is a hyperlocal product-finding MVP. A seeker posts an urgent product request with a reference image, nearby providers discover it on a Leaflet/OpenStreetMap map, respond with real product availability, and the seeker compares images, selects a provider, contacts them and resolves the request.

## Features

- Supabase Auth signup/login for seekers and providers.
- Server-side Flask sessions and role-gated dashboards.
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
flask --app app run --debug
```

Open `http://127.0.0.1:5000`.

## Supabase Setup

1. Create a Supabase project.
2. In Authentication settings, disable email confirmation for this MVP.
3. Run [schema.sql](schema.sql) in the Supabase SQL editor.
4. Create two public storage buckets:
   - `query-images`
   - `response-images`
5. Add storage policies allowing public reads for both buckets.
6. Add authenticated insert policies for both buckets, or rely on the Flask server using the service-role key.

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

A partial unique index prevents duplicate active provider responses for the same query.

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
- State-changing routes require a server-generated CSRF token.
- Flask currently uses service-role database access for server-side table operations. Supabase RLS remains enabled as a protection layer for non-service-role access, but Flask authorization checks are the primary enforcement layer for application requests.
- Provider selection is intended to run through the `select_provider_for_query` PostgreSQL function in `schema.sql` so the matched/rejected transition is atomic. Re-run `schema.sql` after pulling security hardening changes.

## Future Improvements

- Replace polling with Supabase Realtime.
- Add transactional RPC for provider selection.
- Add provider opening hours and inventory categories.
- Add approximate-location controls per individual provider.
- Add server-side tests with mocked Supabase clients.
