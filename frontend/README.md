# Frontend

The Next.js frontend browses the real backend catalog and manages private boards.
The [root quick start](../README.md) can build and start the complete stack with
`docker compose up --build -d`. For frontend hot reload, run the API and data
services using the host workflow there, then from `frontend/`:

```bash
npm ci
npm run dev
```

Use Node 22 (`.nvmrc`). Open http://127.0.0.1:3000. The Next.js server calls
`http://127.0.0.1:8000` by default; copy `.env.example` to `.env.local` to change
`CATALOG_API_URL`. This is a server-only setting. Catalog reads and authenticated
board requests use same-origin Next.js routes, so no browser CORS setup is needed.
A Next.js server is required; this app cannot be exported as static HTML.

## Catalog and boards

Search/filter forms submit native GET requests. Search uses the backend hybrid
ranking. Quotes require a phrase; quotes-only searches use lexical ranking.
Exact creator/location and year filters are sent to the backend. Browse is
newest first, search is relevance order, and pagination follows opaque backend
cursors. Changing search or filters resets pagination.

Asset details show source and rights evidence links, dates and related images.
Image URLs use `/api/catalog/{id}/image/{thumbnail|original}`, which forwards
to the backend visibility-gated private-object read. Responses are uncached and
`next/image` optimization is disabled to prevent cached media from bypassing
later rights checks. Previously delivered bytes cannot be recalled.

To use boards locally, enable the [backend local auth
adapter](../docs/systems/boards.md#local-authentication) in the root `.env`:

```dotenv
APP_ENV=local
AUTH_MODE=local
AUTH_LOCAL_SIGNING_KEY=<a private random value of at least 32 bytes>
```

Restart the API after changing auth configuration. From `backend/`, run
`.venv/bin/python -m app.auth token alice` or `... token bob`. Open **Boards**,
choose the account and paste its one-hour token. The header's **Switch user**
button clears the current board state before another sign-in. There is no
browser token issuer or production sign-in flow. The token is kept in browser
session storage for reloads; boards and pins always come from FastAPI/Postgres.

Create a board on the Boards page. Open a verified catalog work and select
**Save to**; manage notes, order, metadata and removal on the board page. Empty
boards and service/authentication failures have visible states. Board deletion
and pin removal ask for confirmation. The backend remains responsible for
ownership and CC0 eligibility.

Below the pins, **Suggestions for this board** shows recommendations from
`/api/v1/boards/{id}/recommendations`. **More like this board** is the cohesive
mode. **Complement (experimental)** is a placeholder control, not a finished
complement algorithm. A board with no usable pins shows recent works instead.
A note counts saved pins that did not shape the suggestions: pins that are no
longer available, or that have no vector for the API's current embedding model
(for example, Met assets embedded only with OpenCLIP while the API runs the
default `fake` provider). The panel refetches when pins
are added or removed, or when the mode changes; notes and reordering do not
refetch. A newer request aborts the previous one, and a response describing an
older board than the page holds is discarded and requested again. See
[recommendations](../docs/systems/recommendations.md).

`src/api/catalog.ts` owns catalog transport and presentation adaptation;
`src/api/boards.ts` owns board transport and errors. The mounted board provider
holds current-account data, discards stale responses after account switching,
and reconciles successful mutations from API responses. The existing masonry,
guided results, tiles and design tokens remain in use. Archived mock catalog
assets live under `fixtures/`, outside the public tree.

## Validation

```bash
npm test
npm run typecheck
npm run lint
npm run build
```

For real browser tests, run the API with local auth, migrate the database,
ingest and embed at least three eligible assets, and run the production frontend.
The tests use real HTTP, PostgreSQL results and object bytes. The board test
uses CLI tokens; issue both within an hour of running it:

```bash
# From the repository root, with the API on port 8000 and frontend on 3000:
export E2E_ALICE_TOKEN="$(cd backend && .venv/bin/python -m app.auth token alice)"
export E2E_BOB_TOKEN="$(cd backend && .venv/bin/python -m app.auth token bob)"
cd frontend
CATALOG_API_URL=http://127.0.0.1:8000 CATALOG_UI_URL=http://127.0.0.1:3000 npm run test:e2e
```

The catalog browser test covers browse, search, filters, details, provenance,
media and related navigation. The board browser test signs in as Alice, searches
and saves real assets, edits notes and order, reloads, switches to Bob, checks
backend denial of Alice's board UUID, and exercises missing assets and a network
failure. The backend PostgreSQL integration suite additionally checks a real
stored but ineligible asset, rights suppression, and concurrent board writes.
Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to use an installed Chromium binary.
See [board system](../docs/systems/boards.md) and [catalog
system](../docs/systems/catalog-frontend.md) for contracts and limitations.
