import argparse
import json
import subprocess
import sys
from contextlib import asynccontextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chalkline.channels import outbound
from chalkline.core import queries, service


NUMBER = "+12025550109"


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "chalkline-dev"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    service.configure({"business_name": "Mint Tap Repairs", "owner_phones": ["+12025550101"]}, actor="cli")
    return home


@pytest.mark.parametrize(("returncode", "payload", "outcome"), [
    (0, {"success": True}, "sent"),
    (1, {"error": "Twilio API error (400): bad number"}, "failed"),
    (1, {"error": "SMS send failed: "}, "unknown"),
    (0, {"skipped": True}, "failed"),
    (1, {"success": False, "partial_success": True, "error": "Media dropped"}, "unknown"),
    (0, None, "unknown"),
])
def test_cli_outcome_mapping(profile, monkeypatch, returncode, payload, outcome):
    fake = Mock(return_value=subprocess.CompletedProcess([], returncode, json.dumps(payload)))
    monkeypatch.setattr(outbound.subprocess, "run", fake)
    assert outbound._send_text(NUMBER, "Fictional reminder")[0] == outcome
    assert fake.call_args.kwargs["env"]["HERMES_HOME"] == str(profile.parent.parent)


def test_timeout_is_unknown_and_row_is_never_reclaimed(monkeypatch, profile):
    row = service.enqueue_message("other", NUMBER, "Fictional reminder", "test:timeout")
    cli = Mock(side_effect=subprocess.TimeoutExpired("hermes", outbound.SEND_TIMEOUT))
    monkeypatch.setattr(outbound.subprocess, "run", cli)
    assert outbound.deliver_outbox(row["id"]) is None
    cli.assert_not_called()
    service.approve(row["code"], actor="cli")
    assert outbound.deliver_outbox(row["id"]) == "unknown"
    assert queries.get_outbox(row["id"])["status"] == "unknown"
    assert outbound.deliver_outbox(row["id"]) is None
    assert cli.call_count == 2
    assert cli.call_args_list[1].args[0][7] == "sms:+12025550101"


def test_approved_text_uses_real_cli_handler_and_mock_sms_gateway(profile, monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from hermes_cli.send_cmd import cmd_send

    config = GatewayConfig(platforms={Platform.SMS: PlatformConfig(enabled=True)})
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    for key, value in {"TWILIO_ACCOUNT_SID": "ACfictional", "TWILIO_AUTH_TOKEN": "fictional-token",
                       "TWILIO_PHONE_NUMBER": "+12025550100"}.items():
        monkeypatch.setenv(key, value)
    calls = []

    @asynccontextmanager
    async def respond(url, data=None, **_kwargs):
        calls.append((url, {name["name"]: value for name, _headers, value in data._fields}))

        async def body():
            return {"sid": "SMfictional"}

        yield SimpleNamespace(status=201, json=body)

    @asynccontextmanager
    async def session(**_kwargs):
        yield SimpleNamespace(post=respond)

    def run_cli(argv, *, input, timeout, **_kwargs):
        assert argv[:7] == [sys.executable, "-m", "hermes_cli.main", "-p", "chalkline-dev", "send", "--to"]
        assert argv[7] == f"sms:{NUMBER}"
        assert argv[8:] == ["--json", "--file", "-"]
        assert set(_kwargs["env"]) <= {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "LANG", "HERMES_HOME", "PYTHONIOENCODING"} | {k for k in _kwargs["env"] if k.startswith("LC_")}
        assert "FIXTURE_GATEWAY_SECRET" not in _kwargs["env"]
        assert timeout == outbound.SEND_TIMEOUT
        stdout = StringIO()
        with monkeypatch.context() as context, redirect_stdout(stdout), pytest.raises(SystemExit) as exit_info:
            context.setattr("sys.stdin", StringIO(input))
            cmd_send(argparse.Namespace(to=argv[7], message=None, file="-", json=True))
        return subprocess.CompletedProcess(argv, exit_info.value.code, stdout.getvalue())

    monkeypatch.setattr("aiohttp.ClientSession", session)
    monkeypatch.setenv("FIXTURE_GATEWAY_SECRET", "must-not-reach-child")
    monkeypatch.setattr(outbound.subprocess, "run", run_cli)
    row = service.enqueue_message("other", NUMBER, "Fictional reminder", "test:text")
    service.approve(row["code"], actor="cli")
    assert outbound.deliver_outbox(row["id"]) == "sent"
    assert queries.get_outbox(row["id"])["status"] == "sent"
    assert [(url.rsplit("/", 1)[-1], form) for url, form in calls] == [
        ("Messages.json", {"From": "+12025550100", "To": NUMBER, "Body": "Fictional reminder"}),
    ]


def test_sms_never_sends_an_approved_pdf_message(profile, monkeypatch):
    from plugins.plugin_storage import plugin_data_dir

    document = plugin_data_dir("chalkline") / "invoice fixture.pdf"
    document.write_bytes(b"%PDF-1.4\n% fictional invoice\n%%EOF\n")
    row = service.enqueue_message("other", NUMBER, "Fictional invoice", "test:pdf", attachment_path=document)
    service.approve(row["code"], actor="cli")
    send = Mock()
    monkeypatch.setattr(outbound.subprocess, "run", send)
    assert outbound.deliver_outbox(row["id"]) == "failed"
    # Only the owner hears about it: nothing goes to the customer without the approved PDF.
    assert [call.args[0][7] for call in send.call_args_list] == ["sms:+12025550101"]
    assert "SMS cannot carry the PDF" in send.call_args.kwargs["input"]
    stored = queries.get_outbox(row["id"])
    assert stored["status"] == "failed" and "SMS cannot carry the PDF" in stored["detail"]


def test_text_cannot_add_unapproved_attachment(monkeypatch):
    cli = Mock()
    monkeypatch.setattr(outbound.subprocess, "run", cli)
    assert outbound._send_text(NUMBER, "Please see media:/other.pdf")[0] == "failed"
    cli.assert_not_called()
