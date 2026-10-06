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
│   ├── layout.tsx              # Root layout with header/footer
│   ├── page.tsx                # Landing page
│   ├── globals.css             # Global styles
│   ├── archetypes/
│   │   ├── page.tsx            # Archetype gallery
│   │   └── [id]/
│   │       └── page.tsx        # Individual archetype detail pages
│   └── pricing/
│       └── page.tsx            # Pricing page
├── components/
│   ├── Header.tsx              # Site header with nav
│   ├── Footer.tsx              # Site footer
│   └── ArchetypeCard.tsx       # Archetype card component
└── lib/
    ├── presets.ts              # Archetype/genre preset data (24 presets)
    └── pricing.ts              # Pricing tier data
```

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
