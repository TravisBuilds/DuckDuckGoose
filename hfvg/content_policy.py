"""Content-policy word lists (docs/CONTENT-POLICY.md), one copy for every check that applies them."""

import re

# Terms the moderation recovery ladder NEVER rewrites: their presence means a human must look at
# the prompt ("Hard-stop terms"). The built-in default prompt lint profile refuses them up front.
HARD_STOP: frozenset[str] = frozenset({
    "nude", "naked", "nudity", "topless", "undressed", "sexual", "sexy", "erotic", "sensual",
    "seductive", "provocative", "lingerie", "explicit", "intimate", "fetish",
})

# Words the live still precheck refuses (activities.qc.precheck_still_qc, substring match) and the
# G2 still selection repeats; the default prompt lint profile refuses them up front the same way.
PRECHECK_BANNED: tuple[str, ...] = ("nsfw", "explicit", "gore")

# A word as the hard-stop check reads it: hyphenated compounds stay one word ("naked-eye").
WORD_RE = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*")


def hard_stop_terms(text: str) -> list[str]:
    """HARD_STOP terms present in the text (whole words, case-insensitive), sorted."""
    return sorted({w.lower() for w in WORD_RE.findall(text or "")} & HARD_STOP)


def precheck_banned_words(text: str) -> list[str]:
    """PRECHECK_BANNED words the live still precheck would find in the text (substrings,
    case-insensitive), in list order."""
    lowered = (text or "").lower()
    return [w for w in PRECHECK_BANNED if w in lowered]
