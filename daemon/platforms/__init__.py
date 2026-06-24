"""OS audio-playback seam (named 'platforms' to avoid shadowing stdlib platform)."""
from daemon.platforms.base import Platform, make_platform

__all__ = ["Platform", "make_platform"]
