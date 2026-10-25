# Content Policy

Guidelines for acceptable content in the Hands-Free Video Generator.

## Overview

This policy defines what content is acceptable when using the Hands-Free Video Generator. It applies to:
- User-submitted prompts and beatmaps
- Generated stills and videos
- Character descriptions and continuity documents
- Reference images and creative assets

## Core Principles

### 1. Original Content Only

✅ **Allowed:**
- Original characters and settings
- Generic archetypes ("a detective", "an ancient temple")
- Public domain references (Shakespeare, classical mythology)

❌ **Prohibited:**
- Named intellectual property (franchises, brands, copyrighted characters)
- Real people (celebrities, public figures, identifiable individuals)
- Copyrighted locations (specific theme parks, branded venues)

### 2. No Filter Evasion

✅ **Allowed:**
- Creative storytelling within genre conventions
- Cartoon violence (age-appropriate)
- Dramatic conflict

❌ **Prohibited:**
- Attempts to bypass provider content filters
- Rephrasing prohibited content to avoid detection
- Encoding prohibited content in indirect language

### 3. Respectful Representations

✅ **Allowed:**
- Diverse characters and cultures (respectfully portrayed)
- Historical settings (factually accurate)
- Fantasy and fictional cultures

❌ **Prohibited:**
- Stereotypes or caricatures
- Disrespectful cultural appropriation
- Offensive depictions of real cultures or religions

## Detailed Guidelines

### Characters

**Original Archetypes:**
```
✅ Good Examples:
- "A wise elderly mentor"
- "A young adventurer seeking treasure"
- "A talking animal with a secret past"

❌ Bad Examples:
- "Mickey Mouse" (named IP)
- "Harry Potter" (named IP)
- "Elon Musk" (real person)
```

**Generic Descriptions:**
```
✅ Good Examples:
- "A duck wearing a bow tie"
- "A martial arts master in robes"
- "A robot detective"

❌ Bad Examples:
- "Donald Duck" (named IP)
- "Master Yoda" (named IP)
- "R2-D2" (named IP)
```

### Settings and Locations

**Generic Locations:**
```
✅ Good Examples:
- "A mountain village at sunset"
- "An ancient temple in the jungle"
- "A futuristic city with flying cars"

❌ Bad Examples:
- "Hogwarts castle" (named IP)
- "The Death Star" (named IP)
- "Central Park, New York" (real identifiable location - use generic "a city park" instead)
```

### Brands and Products

**Generic Items:**
```
✅ Good Examples:
- "A sports car"
- "A smartphone"
- "A burger restaurant"

❌ Bad Examples:
- "A Tesla Model 3" (brand)
- "An iPhone" (brand)
- "McDonald's" (brand)
```

### Violence and Mature Content

**Age-Appropriate Content:**
```
✅ Allowed:
- Cartoon violence (no gore)
- Dramatic conflict (suspense, danger)
- Mild peril (character in challenging situation)

❌ Prohibited:
- Graphic violence or gore
- Explicit content (sexual or violent)
- Content targeting minors inappropriately
```

### Filter Evasion Attempts

**Examples of filter evasion (PROHIBITED):**
```
❌ "A mouse character who owns a theme park" (clearly Mickey Mouse)
❌ "A boy wizard with a lightning scar" (clearly Harry Potter)
❌ "A green space warrior with pointy ears" (clearly Yoda)
❌ Using character codes to hide IP: "M.M." for Mickey Mouse
❌ Spelling variations: "Mic-key Mo-use"
```

**If a prompt fails content filtering:**
- Rewrite the prompt to be more generic
- Remove specific references
- **DO NOT** try to "trick" the system
- Contact support if you believe the filter is incorrect

## Enforcement

### Automated Checks

The system includes automated checks:

1. **Prompt Lint** (`hfvg/prompt_lint.py`)
   - Runs before estimate / reserve / provider call
   - Live mode: any violation (or a missing lint kit / shot_meta) refuses; dry mode: warn only
   - Clause texts come from the per-episode `prompt_kit.json` `"lint"` section (or `PROMPT_LINT_KIT_PATH`)

2. **Provider Moderation**
   - Higgsfield has built-in content filters
   - ElevenLabs has voice content policies
   - A content block raises the non-retryable `ContentBlockError`

3. **QC Escalation**
   - Problematic content escalates to human review
   - Reviewers can reject content that violates policy

### Manual Review

**Human reviewers may reject content for:**
- Policy violations not caught by automated checks
- Borderline cases requiring judgment
- Repeated attempts to evade filters

### Consequences

The harness has no user accounts; violating content is rejected (lint refusal, provider block,
or human rejection at a gate) and must be rewritten.

## Examples from the Codebase

### Acceptable Content

Use the synthetic `tests/fixtures/sample_beatmap.md` as a neutral example.

### Private Content (not for reuse)

`fixtures/demo/` (BEATMAP.md, CONTINUITY.md, VO-AUDIO.md) and `examples/demo/`
(HARNESS-GATES.json) contain neutral demonstration content for testing and examples.
These are generic templates safe for public distribution.

**DO NOT:**
- Add private or proprietary content to tracked files
- Use it as a template for your own episodes
- Assume it follows all content policy guidelines

## Best Practices

### 1. Start Generic

Begin with generic descriptions, then add unique details:

```
❌ "Spider-Man swings through New York"
✅ "A superhero in red swings through a city using webs"
```

### 2. Focus on Actions, Not Identities

Describe what characters DO, not who they ARE:

```
❌ "Sherlock Holmes solves a mystery"
✅ "A detective with a pipe solves a mystery using logic"
```

### 3. Use Public Domain

Classical mythology, Shakespeare, and folk tales are safe:

```
✅ "Odysseus sails past the sirens" (public domain)
✅ "Romeo and Juliet meet at a ball" (Shakespeare, public domain)
✅ "A trickster fox outsmarts a crow" (Aesop's fable, public domain)
```

### 4. Create Original Characters

Develop your own characters with unique traits:

```
✅ "Quackers the Duck, a former spy turned chef"
✅ "Detective Stag03, a deer who solves forest mysteries"
✅ "Captain Finley, a fish who explores the deep ocean"
```

## Prompt Lint Rules

`lint_prompt(prompt, shot_meta, kit)` checks (exact clause texts come from the episode's lint kit):

1. **Count clause** matching `shot_meta.count` (kit `count_template`)
2. **Locked-camera clause** on exterior / water shots
3. **Real-time / no slow-motion clause**
4. **Forbidden phrases**: default `"nothing else moves"`, `"realistic wild"` (kit may override);
   bare "waves" is forbidden (only "ONE small wave")
5. **No carry/hold verbs** for a moose
6. **Banned decor list** (per kit)
7. **Antler clause** when an antler character is present
8. **Duck clause** when the duck is present

## Moderation Recovery Ladder

When a provider **content-blocks** a still or clip (provider status `nsfw`/`blocked`, classified
`content_block`), the harness recovers one bounded, budget-checked, audited rung at a time
(`hfvg/activities/moderation_recovery.py`). A `canceled` or `failed` job is a provider failure,
not a content block, and never enters the ladder.

1. **Neutral reword**: deterministic, whole-word, case-insensitive. It only *removes* the benign
   descriptors listed below, or swaps them for the plain neutral word shown. It never adds
   spelling tricks, euphemisms, codes, synonyms of the trigger word or new content: every word in
   the reworded prompt is a word of the original prompt or one of the replacement words below.
   If none of the listed words is present the rung is skipped (an identical prompt is never
   resubmitted).
2. **Fallback model** (clips only): re-route to `MODERATION_FALLBACK_MODELS` (default
   `seedance:kling`), validated with the routing planner and the model_caps preflight; any
   routing/preflight error refuses the rung.
3. **Human review**: the shot is flagged `needs_review` with the provider's **exact** message.

A prompt containing a hard-stop term is **never** rewritten (that would be rephrasing prohibited
content to get past the filter, see "No Filter Evasion"): it goes straight to human review.

Every paid rung is pre-checked against the USD round ledger (round and episode 80% stops,
`MAX_PAID_RETRIES_PER_SHOT_PER_ROUND`, default 1) and the resubmit takes its own `-m{n}` job id
and round hold. Each decision is written to `audit_log`: `moderation_block`, `moderation_reword`,
`moderation_reword_skipped`, `moderation_fallback`, `moderation_fallback_refused`,
`moderation_budget_refused`, `moderation_escalated`.

### Neutralized words

Harmless words that trip filters on ordinary scenes (a duck in the rain is "wet", a film shoot,
a "killer" view). Keep the list short and boring; nothing here may be a euphemism for
prohibited content.

| Word | Replacement |
|---|---|
| `wet` | (removed) |
| `soaked` | (removed) |
| `soaking` | (removed) |
| `drenched` | (removed) |
| `dripping` | (removed) |
| `damp` | (removed) |
| `moist` | (removed) |
| `sweaty` | (removed) |
| `steamy` | `misty` |
| `bare` | (removed) |
| `barefoot` | (removed) |
| `exposed` | (removed) |
| `revealing` | (removed) |
| `skin-tight` | (removed) |
| `bloody` | (removed) |
| `gory` | (removed) |
| `killer` | (removed) |
| `shoot` | `film` |
| `shooting` | `filming` |

### Hard-stop terms (never rewritten, always human review)

`erotic`, `explicit`, `fetish`, `intimate`, `lingerie`, `naked`, `nude`, `nudity`, `provocative`, `seductive`, `sensual`, `sexual`, `sexy`, `topless`, `undressed`

## Questions?

If you're unsure whether content violates this policy:

1. **Ask yourself:**
   - Would I recognize this character/setting from existing media?
   - Am I trying to "get around" a filter?
   - Is this respectful to real people and cultures?

2. **When in doubt:** Make it more generic

3. **Contact support:** Open a GitHub Discussion or issue

## Policy Updates

This policy may be updated to:
- Clarify ambiguous cases
- Address new types of content
- Reflect provider policy changes

**Last updated:** 2026-10-19

---

**Remember:** This harness is a tool for creating original content. Use it to tell your own stories, not to recreate existing ones.
