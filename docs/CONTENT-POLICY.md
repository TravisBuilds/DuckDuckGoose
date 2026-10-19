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
   - Checks for forbidden phrases
   - Validates character clauses
   - Enforces prompt structure rules

2. **Provider Moderation**
   - Higgsfield has built-in content filters
   - ElevenLabs has voice content policies
   - Failed content is logged, not retried

3. **QC Escalation**
   - Problematic content escalates to human review
   - Reviewers can reject content that violates policy

### Manual Review

**Human reviewers may reject content for:**
- Policy violations not caught by automated checks
- Borderline cases requiring judgment
- Repeated attempts to evade filters

### Consequences

**For policy violations:**
1. **First violation:** Warning, content rejected
2. **Repeated violations:** Account suspension
3. **Severe violations:** Immediate account termination

## Examples from the Codebase

### Acceptable Content

The repo includes example content that follows these guidelines:

**fixtures/ep05/CONTINUITY.md:**
- Generic character descriptions (no named IP)
- Original character designs
- Public domain references only

**fixtures/ep05/BEATMAP.md:**
- Original story prompts
- Generic locations
- Age-appropriate scenarios

### Unacceptable Content

**Private content (not for public use):**

Located in `examples/mid-mountain-rest/`:
- Specific project with potential IP references
- Not intended as a template for others
- Included for testing purposes only

**DO NOT:**
- Copy content from `examples/mid-mountain-rest/`
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
✅ "Detective Birch, a deer who solves forest mysteries"
✅ "Captain Finley, a fish who explores the deep ocean"
```

## Prompt Lint Rules

The system enforces several prompt structure rules:

### Required Clauses

**Character count:**
```
❌ "Three ducks and a penguin" (doesn't state count)
✅ "3 characters: three ducks and a penguin"
```

**Antler characters:**
```
❌ "Birch enters the room" (antler character without clause)
✅ "Birch [antlers off-screen] enters the room"
```

### Forbidden Phrases

These phrases are blocked by the linter:

- "nothing else moves" (causes motion artifacts)
- "realistic wild" (filter evasion attempt)
- Banned decor words (configurable per-project)

### Character Codes

Use character codes consistently:

```
✅ "[TIKI] walks to the counter"
✅ "[QUACKERS] stirs the pot"

❌ "Tiki walks" then "[TIKI] walks" (inconsistent)
```

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
