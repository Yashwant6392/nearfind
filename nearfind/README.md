# NearFind — Hyperlocal Demand-Driven Discovery & Services Platform

NearFind is a real-time, location-aware web platform that connects people who need urgent items, specialized services, emergency hospital beds, or on-demand rides with nearby verified providers and drivers.

Instead of waiting days for traditional e-commerce delivery or calling dozens of brick-and-mortar stores, NearFind inverts the search paradigm: seekers announce what they need, and nearby providers within a custom radius respond in minutes with real-time availability, photos, and pricing.

---

## Table of Contents

- [Project Overview](#project-overview)
- [Problem Statement & Proposed Solution](#problem-statement--proposed-solution)
- [How It Works](#how-it-works)
- [Key Features](#key-features)
- [Screenshots](#screenshots)
- [Technology Stack](#technology-stack)
- [Architecture & Directory Structure](#architecture--directory-structure)
- [Prerequisites](#prerequisites)
- [Local Installation & Setup (Windows)](#local-installation--setup-windows)
- [Environment Variables](#environment-variables)
- [Supabase Setup & Database Migrations](#supabase-setup--database-migrations)
- [Running Locally](#running-locally)
- [Running Tests](#running-tests)
- [Deployment Instructions (Render & Linux PaaS)](#deployment-instructions-render--linux-paas)
- [Security & Privacy Notes](#security--privacy-notes)
- [Current Limitations & Future Roadmap](#current-limitations--future-roadmap)
- [Contributing & License](#contributing--license)

---

## Project Overview

NearFind is built for college projects, recruiters, and developers looking for a complete, production-ready Flask application integrated with Supabase (PostgreSQL, Auth, Storage) and modern computer vision.

Traditional e-commerce platforms struggle with real-time local inventory. A store two blocks away might have the exact laptop charger, rare medical supply, or tool you urgently need, but because they have no digital storefront, you have no way of knowing without visiting in person.

NearFind bridges this information gap. Using interactive OpenStreetMap/Leaflet geospatial discovery, seekers post queries with images and distance limits. Nearby registered merchants and independent providers view incoming local demand on a live interactive map, submit availability with authentic photos, and negotiate via real-time in-app chat.

---

## Problem Statement & Proposed Solution

### The Problem
1. **Hidden Local Inventory**: Neighborhood shops and independent specialists hold significant stock that search engines and major delivery apps cannot catalog.
2. **Urgency Mismatch**: Standard delivery takes 24 to 72 hours. Urgent needs (medical supplies, replacement hardware, emergency transit) cannot wait.
3. **Inefficient Sourcing**: Seekers waste hours calling shops one by one or traveling blindly from store to store.

### The NearFind Solution
- **Broadcast Demand**: Seekers announce their requirement once, specifying a search radius (e.g., 5 km) and urgency level.
- **Geofenced Notification**: Only providers within that geographic circle receive the opportunity to respond.
- **Visual Verification**: Providers upload photos of the actual in-stock item. A lightweight on-device AI model computes visual similarity against the seeker's reference image to detect mismatches before purchase.
- **End-to-End Coordination**: Built-in chat, turn-by-turn directions, hospital bed status tracking, and on-demand driver dispatch allow users to resolve emergencies quickly.

---

## How It Works

```
  +-------------+              +-----------------------+              +------------------+
  |   Seeker    |              |     NearFind Hub      |              | Nearby Providers |
  +-------------+              +-----------------------+              +------------------+
         |                                 |                                    |
         |  1. Post Request + Photo        |                                    |
         |-------------------------------->|                                    |
         |                                 |  2. Broadcast within Radius (km)   |
         |                                 |----------------------------------->|
         |                                 |                                    |
         |                                 |  3. Submit Availability + Real Pic |
         |                                 |<-----------------------------------|
         |                                 |                                    |
         |  4. Compare Responses & AI Sim  |                                    |
         |<--------------------------------|                                    |
         |                                 |                                    |
         |  5. Select Provider (Atomic)    |                                    |
         |-------------------------------->|                                    |
         |                                 |  6. Notify Provider (Auto-Reject Others)
         |                                 |----------------------------------->|
         |                                 |                                    |
         |  7. In-App Chat, Call & OSM Map |                                    |
         |<===============================>|<==================================>|
         |                                 |                                    |
         |  8. Mark Request Resolved       |                                    |
         |-------------------------------->|                                    |
```

1. **Post a Query**: The seeker enters an item title, description, category, urgency (`normal`, `urgent`, `very_urgent`), search radius (1–50 km), and uploads a reference photo.
2. **Provider Discovery**: Registered providers view open requests within their operational range on an interactive Leaflet map featuring custom pulsing urgency markers.
3. **Provider Response**: A provider clicks "I Have It", specifies price and availability notes, and uploads a photo of their physical product.
4. **Compare & Evaluate**: The seeker's response dashboard polls for new offers, displays price cards, and highlights an AI-computed image similarity percentage.
5. **Atomic Selection**: The seeker selects the best provider. A PostgreSQL database transaction locks the record, shifts the query from `open` to `matched`, flags the winning response as `selected`, and automatically marks competing offers as `rejected`.
6. **Fulfill & Resolve**: The seeker connects with the provider via private in-app messaging, phone call, or WhatsApp, and navigates to the store using OpenStreetMap directions. Once complete, the seeker marks the query `resolved`.

---

## Key Features

### 1. Hyperlocal Demand Matching
- Automatic geolocation detection via browser HTML5 Geolocation API with manual coordinate fallback.
- Server-side Haversine mathematical distance calculation to prevent GPS tampering.
- Filter queries by radius, urgency, category, and real-time status.

### 2. AI Product Image Similarity Estimation
- Integrated with PyTorch and Torchvision using a lightweight MobileNetV3-Small architecture.
- Extracts 576-dimensional feature embeddings on the CPU in milliseconds.
- Computes cosine similarity between reference images and provider stock photos with an in-memory LRU cache.

### 3. Real-Time Communication & Notifications
- Dedicated in-app chat rooms created automatically upon provider selection.
- Read receipt tracking and unread message counters.
- Lifecycle notification feed alerting users of responses, selections, and resolutions.

### 4. Verified Emergency Hospital Bed Directory
- Dedicated `/seeker/emergency-bed` search interface for urgent care.
- Filter hospitals by distance and required bed type: ICU, Ventilator, Oxygen, or General Ward.
- Hospital manager portal allowing medical staff to update real-time bed inventories and manage emergency bed holds.

### 5. On-Demand Driver & Logistics Workflow
- Driver onboarding portal with profile management (experience, licence category, operational radius).
- Secure document upload for Aadhaar identity cards and Driving Licences into private storage.
- Admin reviewer authorization controls preventing unverified drivers from operating.
- Live driver availability publishing, GPS coordinate refresh, and trip matching.
- Seeker ride booking form featuring **Use My Current Location**, distinct pickup (A) and destination (B) interactive map markers, and selectable Optional Amenities (Food, Rest Breaks).

### 6. Robust Security & Session Architecture
- Role-based authorization protecting Seeker, Provider, Hospital Manager, and Driver views.
- Cryptographic CSRF token validation on all state-changing `POST` endpoints.
- Strict multi-layered file validation: verifies file size, declared MIME type, extension, and binary magic bytes (`JPEG`, `PNG`, `WEBP`, `PDF`).

---

## Screenshots

<!-- Add your actual screenshots in a docs/ folder and link them below -->

| Seeker Discovery & Request Creation | Provider Demand Map & Responses |
| :---: | :---: |
| *(Screenshot placeholder: Seeker posting a new query with radius and reference photo)* | *(Screenshot placeholder: Provider viewing pulsing demand markers on Leaflet map)* |

| AI Visual Similarity & Comparison | Emergency Hospital Bed Directory |
| :---: | :---: |
| *(Screenshot placeholder: Side-by-side photo comparison with MobileNetV3 similarity score)* | *(Screenshot placeholder: Verified hospital bed availability with ICU/Oxygen counts)* |

---

## Technology Stack

| Layer | Technologies |
| :--- | :--- |
| **Frontend** | HTML5, Vanilla CSS3 (Custom design system, glassmorphism, responsive grid), Vanilla JavaScript (ES6+), Leaflet.js 1.9.4, OpenStreetMap Tiles |
| **Backend** | Python 3.11+, Flask 3.0.3, Jinja2 template engine, Werkzeug 3.0.3 |
| **Database** | Supabase (PostgreSQL 15+), `pgcrypto`, Row Level Security (RLS), Stored Procedures / RPCs, Triggers |
| **Authentication** | Supabase Auth (JWT email/password) wrapped in secure server-side HTTP-only Flask sessions |
| **Storage** | Supabase Storage (`query-images` and `response-images` public buckets; `driver-documents` private bucket with signed URLs) |
| **Machine Learning** | PyTorch (CPU), Torchvision (MobileNetV3-Small), Pillow, NumPy |
| **Testing** | Pytest 9.1+, 282 comprehensive unit and integration tests |
| **WSGI Servers** | Waitress (Local Windows development), Gunicorn (Production Linux / Render) |

---

## Architecture & Directory Structure

NearFind follows a clean, decoupled Flask architecture:

```text
nearfind/
├── app.py                      # Core Flask application, route controllers, session handling
├── config.py                   # Application configuration, environment validation, constants
├── schema.sql                  # Primary PostgreSQL schema (users, queries, responses, functions)
├── requirements.txt            # Python dependencies (Flask, Supabase, PyTorch, Gunicorn)
├── render.yaml                 # Render Infrastructure-as-Code deployment specification
├── Procfile                    # Standard PaaS process definition
├── .env.example                # Template for required environment variables
├── .gitignore                  # Git ignore rules protecting secrets and sensitive files
│
├── services/                   # Business logic and external service integrations
│   ├── location_service.py     # Haversine distance math, coordinate validation, geocoding
│   ├── similarity_service.py   # MobileNetV3 image embedding and cosine similarity service
│   ├── storage_service.py      # File validation, MIME/magic checking, Supabase Storage uploads
│   └── supabase_service.py     # Resilient Supabase client, error handling, RPC execution
│
├── static/                     # Frontend static assets
│   ├── css/
│   │   └── style.css           # Vanilla CSS design tokens, components, and responsive layout
│   └── js/
│       ├── main.js             # Global utilities, geolocation helpers, alert dismissals
│       ├── seeker-map.js       # Seeker query map and response polling
│       ├── provider-map.js     # Provider demand map and live location tracking
│       ├── hospital-search.js  # Hospital bed search and reservation interactions
│       ├── driver-request-map.js # Seeker ride booking map with pickup/destination pins
│       ├── driver-availability.js# Driver schedule toggle and GPS coordinate refresh
│       ├── navigation.js       # Turn-by-turn routing links and modal direction view
│       └── seeker-reservations.js# Hospital bed hold timers and cancellation actions
│
├── templates/                  # Jinja2 HTML templates
│   ├── base.html               # Master layout with navbar, alerts, and footer
│   ├── index.html              # Public landing page with feature cards and role selector
│   ├── login.html              # User authentication login form
│   ├── signup.html             # Role-based user registration (Seeker vs Provider)
│   ├── error.html              # Standardized user-friendly error display
│   ├── seeker/                 # Seeker views (dashboard, post_query, responses, emergency_bed, driver_request)
│   ├── provider/               # Provider views (dashboard, query details, response submission)
│   ├── hospital/               # Hospital manager portal (dashboard, onboarding)
│   └── driver/                 # Driver views (provider_onboarding, availability)
│
├── migrations/                 # Ordered SQL schema migrations
│   ├── 001_notifications.sql   # User notification tables and triggers
│   ├── 002_chat.sql            # Real-time chat rooms and message history
│   ├── 003_live_location.sql   # Provider live coordinates tracking
│   ├── 004_hospitals.sql       # Hospital profiles and bed availability
│   ├── 005_driver_profiles.sql # Driver profile records and categories
│   ├── 006_driver_verification.sql # Verification document requests and private storage
│   ├── 007_driver_availability_locations.sql # Driver availability shifts and coordinates
│   ├── 008_driver_requests.sql # On-demand ride requests
│   ├── 009_driver_bookings.sql # Driver booking state machine
│   ├── 010_driver_profile_self_approval_guard.sql # Security trigger preventing self-approval
│   ├── 011_driver_workflow_rpc.sql # Driver matching and reservation RPCs
│   ├── 012_users_business_name.sql # Adds business_name field to users
│   ├── 013_hospital_bed_holds.sql # Hospital bed reservation hold workflow
│   └── CONSOLIDATED_003_TO_013.sql # Consolidated script applying migrations 003 through 013
│
└── tests/                      # Automated Pytest suite
    ├── test_security.py
    ├── test_similarity.py
    ├── test_distance_and_driver_profile_audit.py
    ├── test_driver_availability_amenities_ux.py
    ├── test_hospital_and_driver_workflows.py
    └── ... (282 total tests)
```

---

## Prerequisites

Before setting up NearFind locally, ensure you have:

1. **Python 3.11 or newer**: Verify using `python --version` in your terminal.
2. **Git**: Installed and accessible in your command line.
3. **Supabase Account**: A free cloud project at [supabase.com](https://supabase.com).

---

## Local Installation & Setup (Windows)

Follow these step-by-step PowerShell instructions:

### Step 1: Clone the Repository
```powershell
git clone https://github.com/Yashwant6392/nearfind.git
cd nearfind
```

### Step 2: Create and Activate a Python Virtual Environment
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

> **Note**: If PowerShell shows an execution policy error, run:
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

### Step 3: Install Required Dependencies
```powershell
pip install -r requirements.txt
```

### Step 4: Configure Environment Variables
Copy the sample environment file to create your local `.env`:
```powershell
copy .env.example .env
```
Open `.env` in your code editor and populate the variables with your Supabase credentials (see [Environment Variables](#environment-variables)).

---

## Environment Variables

The application reads configuration values through `config.py`. Never commit real secrets to version control.

| Variable | Description | Example / Placeholder |
| :--- | :--- | :--- |
| `FLASK_ENV` | Application environment (`development` or `production`). In production, strict security checks and secure cookie flags are enforced. | `development` |
| `SECRET_KEY` | Cryptographic secret key used to sign session cookies and CSRF tokens. Must be >= 32 characters in production. | `your-super-secret-random-key-min-32-chars` |
| `SUPABASE_URL` | The REST API URL of your Supabase project (from Project Settings -> API). | `https://xyzprojectref.supabase.co` |
| `SUPABASE_ANON_KEY` | Public anonymous API key for public Supabase Auth actions. | `eyJhbGciOi...` |
| `SUPABASE_SERVICE_ROLE_KEY` | Elevated service-role key used securely server-side for database operations and private storage. **Never expose this to frontend code.** | `eyJhbGciOi...` |

---

## Supabase Setup & Database Migrations

NearFind requires a PostgreSQL database with the `pgcrypto` extension and specific tables, functions, and storage buckets.

### 1. Base Schema Setup
1. Log in to your [Supabase Dashboard](https://supabase.com/dashboard) and open your project.
2. Navigate to the **SQL Editor**.
3. Open `schema.sql` from the project root, paste its entire contents into the SQL Editor, and click **Run**.
   - This creates `public.users`, `public.queries`, `public.responses`, Row Level Security (RLS) policies, and the `select_provider_for_query` atomic stored procedure.

### 2. Run Database Migrations in Sequence
Database migrations must be executed in order:

1. **`migrations/001_notifications.sql`**: Creates notifications table, event triggers, and read APIs.
2. **`migrations/002_chat.sql`**: Creates conversation rooms and chat message history for matched seekers/providers.
3. **Advanced Features (Hospitals, Drivers, Bed Holds)**:
   - **Option A (Recommended)**: Run `migrations/CONSOLIDATED_003_TO_013.sql`. This script applies migrations 003 through 013 in one step.
   - **Option B (Incremental)**: Run files `003_live_location.sql` through `013_hospital_bed_holds.sql` one by one in numerical order.

> **Caution**: Do not re-run migrations on a live database where tables already exist. Always verify table existence in Table Editor first.

### 3. Create Storage Buckets
Go to **Storage** in the Supabase Dashboard and configure three buckets:

| Bucket Name | Privacy Level | Purpose | Allowed Files |
| :--- | :--- | :--- | :--- |
| `query-images` | **Public** | Reference photos uploaded by seekers | JPG, PNG, WEBP (Max 5 MB) |
| `response-images` | **Public** | Product photos uploaded by providers | JPG, PNG, WEBP (Max 5 MB) |
| `driver-documents` | **Private** | Aadhaar and Driving Licence verification files | JPG, PNG, PDF (Max 5 MB) |

For `query-images` and `response-images`, add a Public Read policy:
```sql
create policy "Public read query images" on storage.objects for select using (bucket_id = 'query-images');
create policy "Public read response images" on storage.objects for select using (bucket_id = 'response-images');
```
The `driver-documents` bucket must remain strictly **Private**. It is accessed exclusively server-side via time-limited 5-minute signed URLs for authorized reviewers.

---

## Running Locally

### Development Mode (with Flask CLI)
```powershell
python -m flask --app app run
```
The local server starts at: `http://127.0.0.1:5000`

### Local Production Server (with Waitress on Windows)
To simulate a multi-threaded production WSGI server locally:
```powershell
waitress-serve --listen=127.0.0.1:5000 app:app
```

---

## Running Tests

NearFind includes a comprehensive automated test suite covering authentication, role decorators, CSRF validation, Haversine math, state transitions, driver workflows, and emergency hospital holds.

Run all tests from the workspace root:
```powershell
python -m pytest tests/
```

Run a specific test module with verbose output:
```powershell
python -m pytest tests/test_driver_availability_amenities_ux.py -v
```

---

## Deployment Instructions (Render & Linux PaaS)

NearFind is designed for deployment on modern Python hosting platforms such as **Render**, Railway, or AWS.

### Deploying to Render

1. **Fork or Push** your repository to GitHub.
2. In the [Render Dashboard](https://dashboard.render.com), click **New +** and select **Web Service**.
3. Connect your GitHub repository `nearfind`.
4. Render will automatically detect `render.yaml`, or you can configure manually:
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120`
   - **Health Check Path**: `/healthz`
5. Under **Environment Variables**, add:
   - `FLASK_ENV`: `production`
   - `SECRET_KEY`: *(Generate a secure random 64-character string)*
   - `SUPABASE_URL`: *(Your Supabase Project URL)*
   - `SUPABASE_ANON_KEY`: *(Your Supabase Anon Key)*
   - `SUPABASE_SERVICE_ROLE_KEY`: *(Your Supabase Service Role Key)*
   - `PYTHON_VERSION`: `3.11.9`
6. Click **Deploy Web Service**. Render will install dependencies, compile the application, bind Gunicorn to `$PORT`, and verify `/healthz`.

---

## Security & Privacy Notes

- **Credential Isolation**: The `SUPABASE_SERVICE_ROLE_KEY` is kept strictly on the backend. No secret keys or administrative tokens are sent to frontend JavaScript.
- **Private Verification Documents**: Sensitive driver identity records (Aadhaar cards, Driving Licences) are uploaded to a private storage bucket. They cannot be accessed through public URLs; authorized reviewers view them solely via 5-minute cryptographic signed URLs generated server-side.
- **Role-Based Guards**: Every protected route enforces `@seeker_required`, `@provider_required`, `@hospital_manager_required`, or reviewer authorization. Seekers cannot submit provider responses, and providers cannot create seeker queries.
- **State Machine Protection**: Transitions (`open -> matched -> resolved`) use database-level atomic locking (`select_provider_for_query`) to prevent race conditions when multiple providers respond simultaneously.
- **Production Cookie Hardening**: In production mode (`FLASK_ENV=production`), session cookies automatically enforce `Secure=True`, `HttpOnly=True`, and `SameSite=Lax`.

---

## Current Limitations & Future Roadmap

### Current Limitations
- **Polling Updates**: Seeker response updates and driver tracking currently rely on periodic HTTP polling rather than persistent WebSocket connections.
- **CPU Inference**: MobileNetV3 visual similarity runs on CPU, which is suitable for moderate traffic but may need GPU offloading or asynchronous worker queues (Celery/Redis) under heavy concurrency.
- **Mocked Testing**: Automated tests use mocked Supabase tables and storage to run cleanly in CI environments without altering production databases.

### Future Roadmap
- [ ] Migrate real-time response feeds to Supabase Realtime WebSocket subscriptions.
- [ ] Implement integrated payments and escrow holds for driver bookings and merchant orders.
- [ ] Native mobile apps for iOS and Android using React Native or Flutter.
- [ ] Automated SMS/WhatsApp notifications via Twilio for critical urgency alerts.

---

## Contributing & License

Contributions, bug reports, and suggestions are welcome!

1. Fork the Project.
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`).
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`).
4. Push to the Branch (`git push origin feature/AmazingFeature`).
5. Open a Pull Request.

### License
Distributed under the **MIT License**. See `LICENSE` for more information.

### Contact
- **Project Maintainer**: Yashwant Kumar
- **GitHub**: [@Yashwant6392](https://github.com/Yashwant6392)
- **Repository**: [https://github.com/Yashwant6392/nearfind](https://github.com/Yashwant6392/nearfind)
