import re
from collections.abc import Mapping


# Customer intake, owner approvals and sends use the engine's official SMS gateway platform.
CHANNEL = "sms"
PHONE = re.compile(r"\+([1-9][0-9]{1,14})")


def dm_phone(source):
    """Get the E.164 phone identity of a direct SMS chat (sender and chat are the same number)."""
    get = source.get if isinstance(source, Mapping) else lambda field: getattr(source, field, None)
    if get("chat_type") != "dm":
        return None
    sender, chat = get("user_id") or "", get("chat_id") or ""
    match = PHONE.fullmatch(sender)
    if match and chat == sender:
        return f"+{match[1]}"
    return None


def unresolved_sender(source):
    """SMS always carries the sender's phone number, so there is no phone-private identity."""
    return None
