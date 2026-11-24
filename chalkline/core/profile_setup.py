"""Report the profile configuration the business workspace needs.

Provisioning owns the profile config. Chalkline never writes config.yaml; it
only reports the toolset restriction a dedicated business profile should carry.
"""

from hermes_cli.config import load_config

from .. import SLUG

SURFACES = ("sms", "telegram", "cli", "cron")


def required_toolsets() -> dict:
    """Compare the active profile's platform_toolsets with the office-only selection, read-only."""
    try:
        platforms = load_config().get("platform_toolsets")
    except Exception:
        platforms = None
    platforms = platforms if isinstance(platforms, dict) else {}
    needed = {surface: [SLUG] for surface in SURFACES}
    missing = sorted(surface for surface in SURFACES if platforms.get(surface) != [SLUG])
    return {
        "platform_toolsets": needed,
        "missing": missing,
        "applied": not missing,
        "note": "Provisioning owns the profile config. Set these platform_toolsets there; setup does not edit it.",
    }
