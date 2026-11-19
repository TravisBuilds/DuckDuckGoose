"""Shared helpers for the cast and props library tests. Every image is generated here (flat shapes on
a plain background): no real people, no named IP, no brand characters."""

from __future__ import annotations

import io

from hfvg import cast_store, prop_store


def just_over(total: int) -> str:
    """The over-approval refusal when the approval is a millionth of a dollar short of a price in
    whole hundredths of a cent: both figures at four decimals, and that it is just over."""
    assert total % 100 == 0, total
    four = f"${total / 1_000_000:.4f}"
    return f"The estimate is now {four}, just over what you approved ({four}); review it and approve again"


def image_bytes(fmt: str = "PNG", size: tuple[int, int] = (640, 960), color: tuple[int, int, int] = (90, 140, 200),
                seed: int = 0) -> bytes:
    """A placeholder figure (a head and a body as plain shapes); ``seed`` changes the bytes."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, (235, 235, 235))
    draw = ImageDraw.Draw(img)
    w, h = size
    draw.ellipse([w * 0.35, h * 0.08, w * 0.65, h * 0.28], fill=color)
    draw.rectangle([w * 0.3, h * 0.3, w * 0.7, h * 0.85], fill=color)
    draw.rectangle([0, 0, 4 + seed % 50, 4], fill=((seed * 37) % 256, (seed * 11) % 256, 0))
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def attestation(**overrides) -> dict:
    fields = {"rights": "original", "depicts_real_person": False, "likeness_consent": False, "note": ""}
    fields.update(overrides)
    return cast_store.validate_attestation(fields["rights"], fields["depicts_real_person"],
                                           fields["likeness_consent"], fields["note"])


def new_character(library: tuple[str, str] = ("episode", "ep41cast"), name: str = "Pip", **kwargs) -> dict:
    kwargs.setdefault("description", "a small round robot with a teal shell and one yellow antenna")
    kwargs.setdefault("role", "lead")
    return cast_store.create_character(library, name, actor="tester", **kwargs)


def upload(character: dict, version: int = 1, seed: int = 0, **kwargs) -> dict:
    return cast_store.add_upload(character["character_id"], version, image_bytes(seed=seed), filename=f"ref{seed}.png",
                                 attestation=attestation(), actor="tester", **kwargs)


def new_prop(library: tuple[str, str] = ("episode", "ep41cast"), name: str = "Red umbrella", kind: str = "prop",
             **kwargs) -> dict:
    kwargs.setdefault("description", "a red umbrella with a curved wooden handle")
    return prop_store.create_item(library, kind, name, actor="tester", **kwargs)


def new_place(library: tuple[str, str] = ("episode", "ep41cast"), name: str = "Harbour cabin", **kwargs) -> dict:
    kwargs.setdefault("description", "a small blue wooden cabin on stilts at the end of a pier")
    return new_prop(library, name, "place", **kwargs)


def prop_photo(item: dict, seed: int = 0, color: tuple[int, int, int] = (200, 60, 60), **kwargs) -> dict:
    return prop_store.add_photo(item["prop_id"], image_bytes(seed=seed, color=color), filename=f"photo{seed}.png",
                                attestation=attestation(), actor="tester", **kwargs)


def complete_sheet(character: dict, version: int = 1, *, placeholder: bool = False, seed: int = 100) -> dict:
    """A full sheet (master, turnaround, head sheet) as the sheet workflow leaves it."""
    out = {}
    roles = (cast_store.MASTER_ROLE, *cast_store.TURNAROUND_ROLES, cast_store.HEAD_ROLE)
    for i, role in enumerate(roles):
        size = (960, 640) if role == cast_store.HEAD_ROLE else (640, 960)
        out[role] = cast_store.add_generated_image(
            character["character_id"], version, image_bytes(size=size, seed=seed + i), role=role,
            job_id=f"castsheet-{character['character_id']}-v{version}-{role}-a1", placeholder=placeholder,
            actor="tester")
    return out
