"""Check the profile configuration the business workspace needs, read-only.

Provisioning owns the profile config. Chalkline never writes config.yaml. Customer
text is untrusted, so setup and customer intake refuse to run until provisioning has
put the plugin in a dedicated named profile whose surfaces only carry office tools.
"""

from hermes_cli.config import load_config
from hermes_constants import get_hermes_home

from .. import SLUG

SURFACES = ("sms", "telegram", "cli", "cron")


def required_toolsets() -> dict:
    """Compare the active profile with the dedicated, office-only profile Chalkline needs."""
    try:
        home = get_hermes_home()
        dedicated = home.parent.name == "profiles" and home.name != "default"
    except Exception:
        dedicated = False
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
        "dedicated_profile": dedicated,
        "ready": dedicated and not missing,
        "note": ("Provisioning owns the profile config. Run Chalkline in a dedicated named profile "
                 "(not the default) whose config.yaml sets platform_toolsets to exactly "
                 + ", ".join(f"{surface}: [{SLUG}]" for surface in SURFACES)
                 + ". Setup does not edit it."),
    }


def require_ready() -> dict:
    """Refuse unless provisioning has already isolated and restricted this profile."""
    report = required_toolsets()
    if report["ready"]:
        return report
    problems = []
    if not report["dedicated_profile"]:
        problems.append("the active profile is not a dedicated named profile")
    if report["missing"]:
        problems.append("platform_toolsets is not limited to the office tools for: " + ", ".join(report["missing"]))
    raise ValueError("Setup refused: " + "; ".join(problems) + ". " + report["note"])
