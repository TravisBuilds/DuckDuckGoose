"""Deprecated alias of hfvg.compat.species_counter, kept for one release."""
from hfvg.compat.species_counter import *  # noqa: F401, F403, I001
__import__("warnings").warn("hfvg.qc.animal_count moved to hfvg.compat.species_counter", DeprecationWarning, 2)
