import asyncio
import json
import logging
import os
import re
import subprocess
import sys

from hermes_constants import get_hermes_home

from ..core import queries, service
from .identity import CHANNEL, PHONE


logger = logging.getLogger(__name__)
SEND_TIMEOUT = 45
MEDIA_SYNTAX = re.compile(r"MEDIA:|\[\[(?:audio_as_voice|as_document)\]\]", re.IGNORECASE)
_deliveries = set()


def _child_env(root):
    """Minimal environment for the send CLI, so gateway secrets and other profiles' settings don't leak in."""
    keys = ["PATH", "HOME"]
    if os.name == "nt":
        keys += ["USERPROFILE", "SYSTEMROOT", "LANG"]
    env = {key: os.environ[key] for key in keys if key in os.environ}
    if os.name == "nt":
        env.update({key: value for key, value in os.environ.items() if key.startswith("LC_")})
    env.update({"HERMES_HOME": str(root), "PYTHONIOENCODING": "utf-8"})
    return env


def _send_text(recipient, body, attachment_path=None):
    """Send one approved text through the engine's official SMS gateway (`send --to sms:+E164`)."""
    if not PHONE.fullmatch(recipient):
        return "failed", "Invalid SMS recipient."
    if attachment_path:
        # SMS carries text only. Fail closed rather than send the words without the approved PDF.
        return "failed", "SMS cannot carry the PDF. The document is issued; share it with the customer another way."
    if MEDIA_SYNTAX.search(body):
        return "failed", "Message text contains a reserved media directive."

    home = get_hermes_home().resolve()
    root = home.parent.parent if home.parent.name == "profiles" else home
    if home == root:
        profile = "default"
    elif home.parent == root / "profiles":
        profile = home.name
    else:
        return "failed", "Unable to resolve the active workspace profile."
    try:
        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "-p", profile, "send", "--to", f"{CHANNEL}:{recipient}", "--json", "--file", "-"],
            input=body, capture_output=True, encoding="utf-8", errors="replace",
            env=_child_env(root), timeout=SEND_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return "unknown", "The workspace send timed out."
    except OSError:
        return "failed", "Unable to start the workspace send command."

    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        return "unknown", "The workspace send ended without a readable delivery result."
    if not isinstance(payload, dict):
        return "unknown", "The workspace send returned an unexpected delivery result."
    if result.returncode == 0 and payload.get("success") is True and not payload.get("error"):
        return "sent", ""
    if payload.get("partial_success"):
        return "unknown", "Only part of the approved message was delivered."
    error = str(payload.get("error") or "").strip().lower()
    # The standalone SMS sender stringifies exceptions, so a timeout can arrive with an empty detail.
    if "timeout" in error or "timed out" in error or error == "sms send failed:":
        return "unknown", "The SMS gateway timed out."
    if error or payload.get("skipped") or payload.get("success") is False:
        return "failed", "The SMS gateway reported a delivery failure."
    return "unknown", "The SMS gateway did not confirm delivery."


def deliver_outbox(outbox_id, *, now=None):
    """Claim an approved row and record the result of its only send attempt."""
    row = service.claim_outbox(outbox_id, now=now)
    if row is None:
        _deliver_notices(outbox_id, now)
        return None
    try:
        outcome, detail = _send_claimed(row)
    except Exception:
        logger.exception("Delivery interrupted for outbox row %s", outbox_id)
        outcome, detail = "unknown", "Delivery stopped before the result could be recorded."
    service.finish_outbox(outbox_id, outcome, detail=detail, now=now)
    _deliver_notices(outbox_id, now)
    return outcome


def _deliver_notices(outbox_id, now):
    try:
        for notice in queries.delivery_notices(outbox_id):
            deliver_outbox(notice["id"], now=now)
    except Exception:
        logger.exception("Delivery result notice remains queued for outbox row %s.", outbox_id)


def _send_claimed(row):
    return _send_text(row["recipient"], row["body"], row.get("attachment_path"))


def schedule_delivery(outbox_id):
    """Send outside the gateway hook while retaining the active profile context."""
    async def send():
        try:
            await asyncio.to_thread(deliver_outbox, outbox_id)
        except Exception:
            logger.exception("Outbox delivery failed for row %s", outbox_id)

    task = asyncio.create_task(send())
    _deliveries.add(task)
    task.add_done_callback(_deliveries.discard)
