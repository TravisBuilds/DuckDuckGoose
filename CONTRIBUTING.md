# Contributing Guidelines

Thank you for considering contributing to the Hands-Free Video Generator! This document outlines our development practices and requirements.

## Code of Conduct

- Be respectful and constructive
- No harassment or discrimination
- Follow GitHub's Community Guidelines
- Keep discussions professional and on-topic

## Getting Started

1. **Fork the repository**
2. **Clone your fork**
   ```bash
   git clone https://github.com/YOUR_USERNAME/DuckDuckGoose.git
   ```
3. **Set up development environment** (see [docs/LOCAL-DEV.md](docs/LOCAL-DEV.md))
4. **Create a feature branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

## Development Rules

### Critical Rules (Enforced by Mutation Tests)

**NEVER:**
- ❌ Delete tests or mutations
- ❌ Skip tests with `@pytest.mark.skip` (without approval)
- ❌ Weaken test assertions
- ❌ Remove safety checks (budget guards, preflight, auth)
- ❌ Commit secrets or API keys
- ❌ Commit private/branded content

**ALWAYS:**
- ✅ Keep all mutation tests passing (`./scripts/redgreen.sh`)
- ✅ Add tests for new features
- ✅ Run full test suite before committing
- ✅ Use environment variables for secrets
- ✅ Write descriptive commit messages

### One Agent at a Time

**Process rule:** only one coding agent works on the codebase at a time. This is a team
rule, not something the code enforces.

**Separately, the code has an account-level run lock** (`hfvg/activities/run_lock.py`): only one
episode workflow run per account at a time. When starting an episode:
```python
# Workflow acquires account-level lock
await workflow.execute_activity(
    "acquire_run_lock",
    args=[episode_id, workflow_id, "default"],
)
```

**The run lock means:**
- Only ONE episode run per account (`account_identifier`, default `"default"`) at a time
- A second episode start fails while the lock is held
- The lock is released when the workflow ends; a holder that is no longer running, or older
  than `RUN_LOCK_STALE_TIMEOUT_SECONDS` (default 3600), is reclaimed

## Testing Requirements

### Before Committing

```bash
# 1. Smoke tests
pytest tests/test_smoke_test.py

# 2. Full test suite
pytest

# 3. Mutation tests (CRITICAL)
./scripts/redgreen.sh

# All must pass ✓
```

### Adding New Features

**Every new feature requires:**
1. **Unit tests** - Fast, no external dependencies
2. **Integration test** - Real Temporal workflow (if applicable)
3. **Mutation test** - Prove safety check cannot be removed

**Example: Adding a new safety check**

```python
# 1. Add the check
def reserve_budget(amount):
    if spent + reserved + amount > cap:
        raise ValueError("Budget cap exceeded")  # hfvg/budget.py raises ValueError
    # ... reserve logic

# 2. Add test
def test_budget_cap_enforced():
    # Test that cap is enforced
    assert reserve fails when cap would be exceeded

# 3. Add mutation to scripts/redgreen.sh
test_mutation 999 "Budget cap enforced" \
    "hfvg/budget.py" \
    "tests/test_budget.py::test_budget_cap_enforced" \
    sed -i 's/if spent + reserved + amount > cap:/if False:/'
```

### Test Coverage Goals

- **Critical paths:** 100% (budget, auth, safety checks)
- **Workflows:** 90%+ (episode, shot, gates)
- **Activities:** 80%+ (generation, QC, preflight)
- **Utilities:** 70%+

## Code Style

### Python

```python
# Use type hints
def submit_still(episode_id: str, shot_id: str, prompt: str) -> dict[str, Any]:
    """Submit still generation job.
    
    Args:
        episode_id: Episode identifier
        shot_id: Shot identifier
        prompt: Still prompt
        
    Returns:
        dict with job_id, provider_job_id, reserved_amount
        
    Raises:
        InsufficientCreditsError: If cap exceeded
        PreflightError: If shot breaks model limits
    """
    pass

# Use descriptive names
reserved_usd_micros = 100_000  # Good
amt = 100_000  # Bad

# Prefer explicit over clever
if preflight_errors:  # Good
    raise PreflightError(...)
    
if not (not errors and live):  # Bad (double negative)
```

### Linting (Optional but Recommended)

```bash
# Install ruff
pip install ruff

# Check
ruff check hfvg/ api/

# Auto-fix
ruff check --fix hfvg/ api/

# Format
ruff format hfvg/ api/
```

## Commit Message Format

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

**Types:**
- `feat:` - New feature
- `fix:` - Bug fix
- `docs:` - Documentation only
- `style:` - Code style (formatting, no logic change)
- `refactor:` - Code restructuring (no behavior change)
- `test:` - Adding/fixing tests
- `chore:` - Maintenance tasks

**Examples:**
```
feat(budget): add per-user spend caps

Implements per-user budget caps with atomic SQL guards.
Adds USER_CAP_USD_MICROS configuration.

Closes #42

---

fix(preflight): check end-frame refs after anchor drop

End-frame preflight was counting refs before anchor drop,
causing false refusals. Now uses assemble_still_refs output.

Fixes #67

---

test(mutation): add shot retry limit enforcement

Adds M-219 mutation test proving MAX_SHOT_RETRIES is enforced.
```

## Pull Request Process

### 1. Create PR

- Use descriptive title following conventional commits
- Fill out the PR template (if provided)
- Link related issues
- Mark as draft if work in progress

### 2. PR Checklist

Before marking PR as ready:

- [ ] All tests pass locally (`pytest`)
- [ ] Mutation tests pass (`./scripts/redgreen.sh`)
- [ ] No secrets committed (check with `git diff`)
- [ ] Documentation updated (if applicable)
- [ ] CI passes (all 3 jobs green)

### 3. Review Process

- At least one approval required
- Address all review comments
- Keep PR focused (one feature/fix per PR)
- Rebase on main if needed

### 4. Merge

- Use "Squash and merge" for feature branches
- Use "Rebase and merge" for hotfixes
- Delete branch after merge

## Secrets and Credentials

### Never Commit:

- API keys (`HIGGSFIELD_API_KEY`, `OPENAI_API_KEY`, etc.)
- Admin secrets (`ADMIN_SECRET`)
- Private episode data (customer beatmaps, prompts)
- Real provider job IDs or URLs
- Personal information

### Use Environment Variables:

```python
# Good
api_key = os.getenv("HIGGSFIELD_API_KEY")

# Bad
api_key = "abc123:secret456"
```

### Placeholder Values:

```bash
# .env.example (placeholders only)
HIGGSFIELD_API_KEY=
ADMIN_SECRET=CHANGE_ME_MINIMUM_32_CHARACTERS_REQUIRED

# .env (local, gitignored)
HIGGSFIELD_API_KEY=real_id:real_secret
ADMIN_SECRET=<actual-secret>
```

### Check Before Committing:

```bash
# Search for potential secrets
git diff | grep -iE "api.?key|secret|token|password"

# Use git-secrets (optional)
git secrets --scan
```

## Private and Branded Content

### Never Commit:

- Named IP (franchises, brands, characters from media)
- Real people (celebrities, public figures)
- Customer episode data (unless anonymized and approved)
- Private creative assets (character designs, storyboards)

### Demo content in this repo

`examples/demo/HARNESS-GATES.json` (the gate policy loaded by `load_policy("demo")`, the default
`HFVG_PROJECT`) and `fixtures/demo/` (`BEATMAP.md`, `CONTINUITY.md`, `VO-AUDIO.md`, parsed by
`tests/test_episode_parser.py` and `tests/test_preflight_demo_fixtures.py`) are neutral synthetic
content that keeps the structure the code and tests rely on (gate counts, approval points, shot
table columns, tags). Real projects live outside this repo; point the harness at one with
`HFVG_PROJECT` (the policy is read from `examples/<HFVG_PROJECT>/HARNESS-GATES.json`). `tests/test_no_brand_content.py` fails if a denylisted
private name or path shows up in any tracked file or file name.

### If You Need Examples:

Use generic, original content:
```
# Good
"A duck teaches cooking"
"An ancient temple in mountains"

# Bad (named IP)
"Mickey Mouse teaches cooking"
"Hogwarts castle in mountains"
```

## Workflow Safety Rules

### Budget and Spending

**Every paid operation must:**
1. Reserve budget atomically (SQL WHERE clause)
2. Use idempotent job_id (includes version/attempt)
3. Poll with bounded retries (~30 min)
4. Commit or release (never both)
5. Log all operations (reserve, commit, release)

**Example:**
```python
# Good: Atomic reserve with cap check
UPDATE budget_lines
SET reserved_usd_micros = reserved_usd_micros + ?
WHERE line_id = ?
  AND (spent_usd_micros + reserved_usd_micros + ?) <= stop_usd_micros

# Bad: Check then reserve (race condition)
current = get_current_total()
if current + amount <= cap:
    reserve(amount)  # Another process could reserve in between!
```

### Gate Approvals

**Never bypass gates:**
- G1 (shot plan) required before stills
- G2 (scene stills) required before clips
- G_FINAL_CUT (final cut) required before delivery

**Validators are mandatory:**
```python
# Good: Update with validator
@workflow.update
async def approve_g2_scene_stills(self):
    self.approved_g2_scene_stills = True

@approve_g2_scene_stills.validator
def validate_approve_g2_scene_stills(self):
    if self.state.pending_retries:
        raise ApplicationError("Cannot approve while retry pending")

# Bad: Signal without validation (validator gets ignored)
@workflow.signal
async def approve_g2_scene_stills(self):
    self.approved_g2_scene_stills = True  # No validation!
```

### Preflight Validation

**All shots must pass preflight before spend:**
- G1 plan validates entire episode
- Submit activities validate per-shot
- PreflightError is non-retryable

**Never skip preflight:**
```python
# Good: Preflight before reserve
preflight.raise_on_errors(violations, shot_id)
reserved = await reserve_budget(amount)

# Bad: Skip preflight (overspend risk, broken shots)
reserved = await reserve_budget(amount)  # No validation!
```

## Documentation

### Required Documentation Updates:

**When adding a feature:**
- Update README.md (if user-facing)
- Update docs/ARCHITECTURE.md (if changing design)
- Update API docs (if adding endpoint)
- Add docstrings to new functions/classes

**When fixing a bug:**
- Add test that reproduces the bug
- Document the fix in commit message

**When changing configuration:**
- Update .env.example
- Update docs/LOCAL-DEV.md
- Document new env vars in README.md

## Reporting Issues

### Bug Reports

Include:
- Description of the bug
- Steps to reproduce
- Expected behavior
- Actual behavior
- Environment (OS, Python version, Temporal version)
- Logs/error messages
- Episode ID (if applicable)

### Feature Requests

Include:
- Description of the feature
- Use case / motivation
- Proposed solution (optional)
- Alternatives considered (optional)

### Security Issues

**Do NOT open public issues for security vulnerabilities.**

Contact maintainers privately:
- Email: [security contact - TBD]
- Include: Vulnerability description, impact, reproduction steps

## Questions?

- Check [docs/LOCAL-DEV.md](docs/LOCAL-DEV.md) for setup
- Check [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for design
- Open a [Discussion](../../discussions) for questions
- Open an [Issue](../../issues) for bugs/features

## License

By contributing, you agree that your contributions will be licensed under the same license as the project (see [LICENSE](LICENSE)).

---

**Thank you for contributing to the Hands-Free Video Generator!** 🎬
