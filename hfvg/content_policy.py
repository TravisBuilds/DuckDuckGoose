"""Content-policy word lists (docs/CONTENT-POLICY.md), one copy for every check that applies them."""

# Terms the moderation recovery ladder NEVER rewrites: their presence means a human must look at
# the prompt ("Hard-stop terms"). The built-in default prompt lint profile refuses them up front.
HARD_STOP: frozenset[str] = frozenset({
    "nude", "naked", "nudity", "topless", "undressed", "sexual", "sexy", "erotic", "sensual",
    "seductive", "provocative", "lingerie", "explicit", "intimate", "fetish",
})

# Words the live still precheck refuses (activities.qc.precheck_still_qc, substring match) and the
# G2 still selection repeats; the default prompt lint profile refuses them too (whole words).
PRECHECK_BANNED: tuple[str, ...] = ("nsfw", "explicit", "gore")
