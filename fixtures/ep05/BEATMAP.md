# Ep05 Teaser "Yuzu Bath" (Quacked Concierge / Mid-Mountain Rest): BEATMAP v0 DRAFT · owner Travis Wu · 2026-10-15 CEST · 0 credits spent · DOCS ONLY, nothing generated · never post (GX.01)

**Runtime target:** ≈20–25 s → this map: **24.0 s** (4 shots, no dissolves) · gen total 25 s.
**🔒 Higgsfield cap: 500 credits (Travis).** Plan below: **240.5 cr** incl. reserve (first pass 140.5).
First fully end-to-end DuckDuckGoose harness run. Status: awaiting Travis lock (G1.03 / G1.08).

**Format:** shot rows use the handbook/Ep03/Ep04 row format read by `hfvg.episode_parser.parse_beatmap` (`| # | ID | Screen (gen) [⇥] | Room | TOD | Chars | Action | Purpose | In | Out | Model |`); the extra columns (N, duck state, ambient, credits) follow after Model, like Ep04. ⇥ = START + END frame clip. Screen = length in the cut; gen = generated length (Kling min 3 s).
**Characters:** R = Rowan (dad) · H = Hazel (mom) · M = Mochi (pup) · D = the duck, host only (DUCK-AB02: short broad bill, cream beanie with NO pompom, cream-and-green striped vest). Names are gentle short English names, chosen here (draft; Travis may rename).
**Model:** every shot has more than one animal → **K = Kling 3.0 pro, locked first frame**, sound off, 9:16 1080×1920, 24 fps.

## 1. Rules for every row
- **Same resort:** the pool is the Ep03 **R7 Spring wing** (`ep03-snow-monkeys/refs/rooms/R7-SPRING-WING.png`), the series open-air pool language (timber deck + resort timber railing, geodesic timber-glass dome glowing amber, black-iron post lanterns, snowy granite, fjord/mountain view). Set "exactly as plate R7". **No Japanese-bathhouse drift**: no ryokan, shoji, tōrō, wooden-tub bathhouse, noren on the pool. Yuzu and the tea tray are PROPS only (Ep03 CONTINUITY §0).
- **Exactly 3 capybaras in every shot** (N column = 3 capybaras; prompt says "exactly three capybaras"). NatGeo-photoreal, real wet fur, real capybara behaviour only (dozing, eyes half-closed, ear flicks, nose twitch, chewing). No upright stance, no paws used as hands.
- **Duck = host, never in the water**, always on the poolside deck. Understated gestures (set tray, tiny nod, step back); never frozen, never touches a guest, never splashes.
- **Every shot animatable** with named ambient motion (steam, drifting yuzu, breath, snowflakes). Locked camera, static sky, no timelapse, bgdrift gate (G3.04).
- **Cuts:** every cut changes angle or shot size. Entrances/exits are motivated on screen (duck walks in along the deck from the dome door, frame LEFT, and steps back the same way).

## 2. Shot table
| # | ID | Screen (gen) s, ⇥ if end frame | Room | TOD | Chars (codes) | Action | Purpose | In | Out | Model | N | Duck state | Ambient motion | Cr video | Cr stills |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | A01 | 5.5 (6) | Open-air warm pool (set exactly as plate R7 spring wing), deck edge in frame RIGHT | Winter late afternoon, soft snowfall | R H P | WIDE, locked, eye level from the far rock side. Exactly three capybaras doze chin-deep in the warm pool, eyes half-closed, side by side (Rowan left, Hazel middle, Mochi right against her); about 12 yuzu drift slowly; empty deck at right | Establish guests, pool, yuzu, calm | Fade from black (0.5 s) | Cut, angle change to the deck | K | 3 | absent | Steam rising, yuzu drifting, slow snowflakes, ear flicks, breath | 12 | 9 |
| 2 | A02 | 5.0 (5) ⇥ | Same pool, the poolside timber deck edge (plate R7) | Same, continuous | D R H P | MEDIUM from the deck, over the pool. The duck walks in from frame LEFT along the deck (from the dome door), silently sets a small tea tray on the deck right at the pool edge, gives ONE tiny nod to the dozing guests and steps back out LEFT. The capybaras keep dozing in soft focus | The host serves without disturbing | Cut | Cut, size change to close-up | K | 3 | host, beanie on, on the deck only | Steam drifting across the deck, yuzu bobbing, duck breathes, beanie stays still | 10 | 18 |
| 3 | A03 | 4.0 (4) | Same pool, water level (plate R7) | Same | P H R | CLOSE-UP at water level on Mochi: he opens ONE eye, noses a drifting yuzu and nibbles it (one small bite, real chewing); Hazel's cheek in frame edge, Rowan soft behind; the tea tray soft on the deck edge in background | The tiny payoff | Cut | Cut, angle + size change to high wide | K | 3 | absent (tray only) | Steam, ripples from the nibble, water droplets on whiskers, nose twitch | 8 | 9 |
| 4 | A04 | 9.5 (10) | Same pool and deck, high wide from the deck end by a post lantern (plate R7) | Same, lanterns lit, dome glowing amber | D R H P | ENDING (Ep01 template, teaser length): one continuous calm take. The duck stands back on the deck by the lantern, tea tray at the pool edge, watching calmly with a gentle head turn; the three capybaras doze, Mochi still chewing his nibbled yuzu. Logo: white text watermark, 62 % width, centred, 2.5 s fade-in from A04 +3.0 s, visible the last 6.5 s | Calm resolve under the logo | Cut | End (calm take under the logo) | K | 3 | host, beanie on, on the deck | Steam, snowfall, yuzu drift, lantern flicker, duck breathes | 20 | 9 |

## 3. Cost estimate (no spend; rates: Kling 3.0 pro **2 cr/s** per `ep04-moose/drafts/video/BUDGET-v2.md` §3 "Kling wet 14 s × 2"; stills 9 per frame = 2.5 draft + 6.5 final, Ep04 BEATMAP §8)
| Shot | Video | Stills | Total |
|---|---|---|---|
| A01 | 6 s × 2 = 12 | 1 frame = 9 | 21 |
| A02 ⇥ | 5 s × 2 = 10 | 2 frames = 18 | 28 |
| A03 | 4 s × 2 = 8 | 9 | 17 |
| A04 | 10 s × 2 = 20 | 9 | 29 |
| **Shots** | **50** | **45** | **95** |
| Refs (harness-made): capybara sheets Rowan/Hazel/Mochi 3 × 6.5 + family lineup 6.5 + prop sheet (tea tray, yuzu) 6.5 + 2 re-rolls 13 | | | **45.5** |
| **First pass** | | | **140.5** |
| Revision reserve (Kling re-rolls, still fixes) | | | 100 |
| **Plan total** | | | **240.5 / cap 500** (80 % stop = 400) |
Note: Ep04 BEATMAP §8 used 1.5 cr/s for Kling; at that rate video is 37.5 (first pass 128). 2 cr/s is the conservative newer figure. No new pool plate (R7 reused). Balance preflight (GC.01) not read (no spend in this step).
