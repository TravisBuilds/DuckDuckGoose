"""Real provider adapters for Higgsfield and ElevenLabs."""

from hfvg.providers.base import GenerationProvider, ProviderJob, ProviderJobStatus
from hfvg.providers.higgsfield import HiggsfieldProvider
from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
from hfvg.providers.kling_video import KlingVideoProvider
from hfvg.providers.elevenlabs import ElevenLabsProvider

__all__ = [
    "GenerationProvider",
    "ProviderJob",
    "ProviderJobStatus",
    "HiggsfieldProvider",
    "HiggsfieldStillProvider",
    "KlingVideoProvider",
    "ElevenLabsProvider",
]
