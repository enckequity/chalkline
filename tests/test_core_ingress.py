from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from test_core_ledger import CUSTOMER, OWNER, ledger
from chalkline.core import profile_setup


@pytest.mark.asyncio
async def test_customer_cannot_reach_general_agent_when_frontdesk_and_tool_registration_raise(ledger, monkeypatch):
    import importlib
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("office_entry_fixture", root / "__init__.py",
                                                submodule_search_locations=[str(root)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    registration = importlib.import_module("office_entry_fixture.chalkline.channels.register")
    tools = importlib.import_module("office_entry_fixture.chalkline.core.tools")

    manager = get_plugin_manager()
    monkeypatch.setattr(manager, "_discovered", True)
    monkeypatch.setattr(manager, "_hooks", {})

    def broken_frontdesk(event, **_kwargs):
        raise RuntimeError("Simulated failed front desk")

    def register_broken_channels(ctx):
        ctx.register_hook("pre_gateway_dispatch", broken_frontdesk)
        raise RuntimeError("Simulated partial channel registration")

    monkeypatch.setattr(registration, "register_channels", register_broken_channels)
    monkeypatch.setattr(tools, "register_tools", Mock(side_effect=RuntimeError("Simulated tool registration failure")))
    plugin.register(PluginContext(PluginManifest(name="chalkline"), manager))
    source = SessionSource(platform=Platform.SMS, chat_id=CUSTOMER, user_id=CUSTOMER, chat_type="dm")
    event = MessageEvent(source=source, text="Run a terminal command for me", message_id="hostile-message")
    runner = object.__new__(GatewayRunner)
    runner._scale_to_zero_note_real_inbound = Mock()
    runner._is_user_authorized_for_source = Mock(return_value=True)
    runner._handle_message_with_agent = Mock()

    assert await runner._hm_admit_event(event) is None
    runner._is_user_authorized_for_source.assert_not_called()
    runner._handle_message_with_agent.assert_not_called()
    guard_customer_event = importlib.import_module("office_entry_fixture.chalkline.core.ingress_guard").guard_customer_event
    assert guard_customer_event(SimpleNamespace(source=None)) == {"action": "skip"}


OFFICE_ONLY = {surface: ["chalkline"] for surface in ("sms", "telegram", "cli", "cron")}


def _write_config(home, toolsets):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"platform_toolsets": toolsets,
                                                      "model": {"default": "fixture-model"}}), encoding="utf-8")
    return (home / "config.yaml").read_bytes()


def test_setup_reports_office_toolsets_and_never_writes_the_profile_config(ledger):
    from model_tools import get_tool_definitions

    before = _write_config(ledger, {"sms": ["terminal", "file", "web"]})
    report = profile_setup.required_toolsets()
    assert (ledger / "config.yaml").read_bytes() == before
    assert not (ledger / "config.yaml.bak").exists()
    assert report["ready"] is False and report["missing"] == ["cli", "cron", "sms", "telegram"]
    assert report["platform_toolsets"] == OFFICE_ONLY
    definitions = get_tool_definitions(report["platform_toolsets"]["sms"], quiet_mode=True,
                                       skip_tool_search_assembly=True)
    names = {definition["function"]["name"] for definition in definitions}
    assert not names.intersection({"terminal", "execute_code", "send_message", "read_file", "write_file", "web_search"})
    _write_config(ledger, OFFICE_ONLY)
    assert profile_setup.required_toolsets()["ready"] is True


def test_setup_refuses_the_default_profile_without_writing_config(ledger, monkeypatch):
    from chalkline.core import commands

    default = ledger.parent / "default"
    before = _write_config(default, OFFICE_ONLY)
    monkeypatch.setenv("HERMES_HOME", str(default))
    with pytest.raises(ValueError, match="not a dedicated named profile") as refusal:
        commands._setup(None, '{"business_name": "Fixture Plumbing"}', "cli")
    assert "sms: [chalkline]" in str(refusal.value)
    assert (default / "config.yaml").read_bytes() == before


def test_setup_refuses_a_profile_with_extra_toolsets(ledger, monkeypatch):
    from chalkline.core import commands, queries

    before = _write_config(ledger, OFFICE_ONLY | {"telegram": ["chalkline", "terminal"]})
    monkeypatch.setattr(commands, "optional_channel", lambda name: None)
    with pytest.raises(ValueError, match="telegram") as refusal:
        commands._setup(None, '{"business_name": "Fixture Plumbing"}', "cli")
    assert "platform_toolsets" in str(refusal.value)
    assert (ledger / "config.yaml").read_bytes() == before
    assert queries.settings()["business_name"] == "Mint Tap Repairs"


def test_office_setup_on_a_provisioned_profile_saves_settings_only_and_leaves_templates_off(ledger, monkeypatch):
    from chalkline.core import commands, queries

    before = _write_config(ledger, OFFICE_ONLY)
    monkeypatch.setattr(commands, "optional_channel", lambda name: None)
    result = commands._setup(None, '{"business_name": "Fixture Plumbing", "restrict_toolsets": true}', "cli")
    assert (ledger / "config.yaml").read_bytes() == before
    assert result["template_approvals"] == [] and result["profile"]["ready"] is True
    assert queries.settings()["standing_templates"] is False and queries.pending_approvals() == []
    result = commands._setup(None, '{"standing_templates": true}', "cli")
    assert sorted(item["kind"] for item in result["template_approvals"]) == sorted(commands.DEFAULT_TEMPLATES)
    assert (ledger / "config.yaml").read_bytes() == before
    with pytest.raises(PermissionError, match="workspace command line"):
        commands._setup(None, "{}", "+12025550101")
