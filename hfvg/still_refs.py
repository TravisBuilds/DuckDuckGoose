"""The lead/anchor ref assembly rule for stills, shared by submit and preflight (pure, no I/O)."""


def assemble_still_refs(
    lead: str | None,
    anchor: str | None,
    planned: list[str],
    cap: int,
) -> tuple[list[str], bool]:
    """
    Assemble still refs in priority order: [lead] + [anchor] + planned.
    When lead + anchor + planned > cap, the anchor is dropped first (lead takes priority).
    Nothing else is ever dropped: if still over cap the result exceeds cap and the caller must refuse.

    Returns:
        (assembled_refs, anchor_used)
    """
    planned = list(planned or [])
    refs = ([lead] if lead else []) + ([anchor] if anchor else []) + planned
    anchor_used = bool(anchor)
    if lead and anchor and len(refs) > cap:
        refs = [lead] + planned
        anchor_used = False
    return refs, anchor_used
