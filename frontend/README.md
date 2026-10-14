# Frontend

Mock browse UI. Images are Open Access files from The Metropolitan Museum of Art (CC0), stored in `public/works/`. Boards live in this browser's session storage.

From `frontend/`:

```bash
npm ci
npm run dev
```

Open http://127.0.0.1:3000. Search filters the mock catalog. A work page shows the author, publishing date, and related works. Boards can save and reorder pins until the API exists.

Keep the board provider when persistence arrives: replace its sessionStorage
operations with API calls without discarding the provider. Retain a small
`public/works/` fixture; `next/image` optimizes these local images. Production
image bytes will come from object storage. When switching to public object URLs,
configure a narrow `images.remotePatterns` rule for that host and path; private
or signed URLs need a loader or an unoptimized image because the default image
optimizer does not forward authentication headers.

[design/tokens/tokens.css](../design/tokens/tokens.css) is the token source of
truth. Tailwind `@theme` in `src/app/globals.css` maps those variables.
Component tests wait until interaction-heavy UI needs them.
