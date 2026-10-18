"""Test-only: let tests that upload fake (non-image) bytes bypass reference normalization.

Product code has NO switch to skip normalization (a live worker can never skip it). Tests
that feed fake bytes to upload_reference call passthrough_normalize(); the autouse fixture in
tests/conftest.py restores the real function after every test, so nothing leaks.
"""
from pathlib import Path

import hfvg.image_normalize as _norm

REAL_NORMALIZE = _norm.normalize_reference_image


def _passthrough(source_path, cache_dir=None):
    p = Path(source_path)
    return (p, _norm.compute_file_sha256(p))


def passthrough_normalize() -> None:
    _norm.normalize_reference_image = _passthrough


def restore_normalize() -> None:
    _norm.normalize_reference_image = REAL_NORMALIZE
