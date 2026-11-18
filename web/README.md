# DuckDuckGoose Creator Website

A Next.js website for the DuckDuckGoose hands-free AI video generation harness.

## Tech Stack

- **Next.js 16** (App Router)
- **TypeScript**
- **Tailwind CSS**
- **Static export ready** (no required environment variables)

## Development

```bash
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) to view the site.

## Build

```bash
npm run build
```

The build produces a static-export-ready app with no required environment variables.

## Vercel Deployment

When importing this project to Vercel:

1. **Framework Preset**: Next.js
2. **Root Directory**: `web`
3. **Build Command**: `npm run build` (default)
4. **Output Directory**: `.next` (default)
5. **Environment Variables**: None required

The site is fully static-friendly and builds without any external API calls.

## Project Structure

```
web/
├── app/
│   ├── layout.tsx              # Root layout with header/footer (not on the home page)
│   ├── page.tsx                # Public home page (components/home/)
│   ├── globals.css             # Global styles
│   ├── archetypes/
│   │   ├── page.tsx            # Archetype gallery
│   │   └── [id]/
│   │       └── page.tsx        # Individual archetype detail pages
│   └── pricing/
│       └── page.tsx            # Pricing page
├── components/
│   ├── home/                   # The home page's sections, header, footer and deck shapes
│   ├── Header.tsx              # Site header with nav
│   ├── Footer.tsx              # Site footer
│   └── ArchetypeCard.tsx       # Archetype card component
├── lib/
│   ├── home-copy.mjs           # Every word on the home page
│   ├── copy-rules.mjs          # Banned words and off-limits topics for public copy
│   ├── home-pictures.mjs       # The home page's photos and their sizes
│   ├── presets.ts              # Archetype/genre preset data (24 presets)
│   └── pricing.ts              # Pricing tier data
├── public/home/                # The home page's photos as AVIF and WebP
└── scripts/home-pictures.mjs   # Makes public/home/ from the direction deck's photos
```

## Home page

`/` is the same static page for everyone. Its "Sign in" and "Get early access" links open `/studio`:
middleware lets a signed-in visitor straight in and sends anyone else to `/login?from=%2Fstudio`,
which returns them to the studio after signing in. Logging out of the studio lands back on `/`.
There is no public sign-up yet, so "Get early access" leads to sign-in (`EARLY_ACCESS_HREF` in
`components/home/links.ts`, marked TODO).

All of its text lives in `lib/home-copy.mjs`. `lib/home-copy.test.mjs` (in `npm run test:unit`) fails
if that text uses a banned word or an off-limits topic from `lib/copy-rules.mjs`, if a home component
writes text of its own, or if an icon or symbol character appears; `tests/home-page-e2e.spec.ts`
checks the rendered page and the sign-in routes.

To change a photo, replace it in the deck and run `node scripts/home-pictures.mjs <deck>/ppt/media`.

## Content Policy

All archetype content is original or uses public-domain references only. No named IP, franchises, brands, or real people appear in the content. Any references from the original draft presets have been rewritten to be generic and original.

## TODOs for Backend Integration

- [ ] Connect "Create video" button to brief submission endpoint
- [ ] Wire up email waitlist form to backend
- [ ] Add OAuth sign-in (Google/Apple)
- [ ] Implement credit ledger integration
- [ ] Add user dashboard for tracking videos
- [ ] Connect to actual video generation pipeline

## License

MIT
