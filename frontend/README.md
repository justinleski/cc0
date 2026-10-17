# Frontend

The Next.js frontend browses the real backend catalog. Run the API and data
services using the [root quick start](../README.md), then from `frontend/`:

```bash
npm ci
npm run dev
```

Use Node 22 (`.nvmrc`). Open http://127.0.0.1:3000. The server calls
`http://127.0.0.1:8000` by default; copy `.env.example` to `.env.local` to change
`CATALOG_API_URL`. This is a server-only setting, with no browser credentials
or CORS configuration needed. A Next.js server is required; this slice cannot
be exported as static HTML.

Search/filter forms submit native GET requests. Search uses the existing hybrid backend ranking. Quotes require a phrase;
quotes-only searches use lexical ranking. Exact creator/location and year
filters are sent to the backend. Browse is newest first, search is relevance
order, and pagination follows opaque backend cursors. Changing search or filters
resets pagination. Creator lookup provides up to 50 choices; narrow by name
when necessary. No client-side search, ranking or filter implementation exists.

Details show source and rights evidence links, verification/ingestion dates,
and related images from the active embedding model. Image URLs use the same-origin
route `/api/catalog/{id}/image/{thumbnail|original}`, which forwards to the
backend's visibility-gated private-object read. Responses are uncached and
`next/image` optimization is disabled to prevent cached media from bypassing
subsequent rights checks. Previously delivered bytes cannot be recalled.

`src/api/catalog.ts` owns HTTP and response adaptation; `catalog-query.ts` owns
URL parameters. Components receive presentation models. The existing masonry,
guided results, tiles, and design tokens are retained. Archived mock data and
images live under `fixtures/`, outside the public tree. Mock board pages are
replaced with a deferred-feature message; the session board provider is retained
for future work but is not mounted. No authentication or persistent boards exist.

```bash
npm test             # API and component tests, no running backend needed
npm run typecheck
npm run lint
npm run build
```

For a real browser check, start the API and frontend, ingest and embed at least
two eligible assets (`backend/.venv/bin/python -m app.worker ingest` from the
repository root), then:

```bash
npx playwright install chromium
CATALOG_API_URL=http://127.0.0.1:8000 CATALOG_UI_URL=http://127.0.0.1:3000 npm run test:e2e
```

The browser test uses real HTTP endpoints, PostgreSQL results and object bytes;
it does not intercept requests or modify catalog records. It covers browse,
pagination, quoted search, exact filters, empty results, details, provenance,
originals, related navigation and missing assets. It saves a provenance screenshot
to `test-results/catalog-provenance.png`. Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to
use an already-installed Chromium binary if needed.

See [catalog system](../docs/systems/catalog-frontend.md) for contracts, architecture
and limitations. [design tokens](../design/tokens/tokens.css) remain the visual
source of truth.
