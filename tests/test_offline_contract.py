"""Offline host-contract probes against the installed Hermes source."""

import argparse
import json
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "chalkline-dev"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.mark.asyncio
async def test_customer_event_with_media_can_be_silently_consumed(profile):
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

    customer = "+12025550109"
    source = SessionSource(platform=Platform.SMS, chat_id=customer, user_id=customer, chat_type="dm")
    event = MessageEvent(
        source=source, text="Fictional booking request", message_id="g2-customer",
        media_urls=[str(profile / "receipt.png"), str(profile / "voice.ogg")],
        media_types=["image/png", "audio/ogg"],
    )
    seen = []

    def consume(event, **_context):
        seen.append((event.source.user_id, event.text, event.media_types, event.media_urls))
        return {"action": "skip", "reason": "offline customer fixture"}

    manager = get_plugin_manager()
    manager._discovered = True
    ctx = PluginContext(PluginManifest(name="chalkline"), manager)
    ctx.register_hook("pre_gateway_dispatch", consume)
    runner = object.__new__(GatewayRunner)
    runner._scale_to_zero_note_real_inbound = Mock()
    runner._is_user_authorized_for_source = Mock()

    assert await runner._hm_admit_event(event) is None
    assert seen == [(customer, event.text, event.media_types, event.media_urls)]
    runner._is_user_authorized_for_source.assert_not_called()


def test_send_cli_routes_proactive_text_to_the_official_sms_gateway(profile, monkeypatch, capsys):
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
        calls.append((url.rsplit("/", 1)[-1], {name["name"]: value for name, _headers, value in data._fields}))

        async def body():
            return {"sid": "SMfictional"}

        yield SimpleNamespace(status=201, json=body)

    @asynccontextmanager
    async def session(**_kwargs):
        yield SimpleNamespace(post=respond)

    monkeypatch.setattr("aiohttp.ClientSession", session)
    number = "+12025550109"
    with pytest.raises(SystemExit) as exit_info:
        cmd_send(argparse.Namespace(to=f"sms:{number}", message="Fictional reminder", json=True))
    assert exit_info.value.code == 0, capsys.readouterr()
    assert json.loads(capsys.readouterr().out)["success"]
    assert calls == [("Messages.json", {"From": "+12025550100", "To": number, "Body": "Fictional reminder"})]


def test_single_recurring_job_can_dispatch_a_profile_database_tool(profile):
    from cron.jobs import get_job, list_jobs
    from cron.scheduler import _construct_cron_agent, _CronAgentSetup
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
    from model_tools import get_tool_definitions, handle_function_call
    from plugins.plugin_storage import plugin_db
    from tools import cronjob_tools  # Registers the built-in cron tool.

    slug, tool = "chalkline", "chalkline_g2_due_fixture"
    with closing(plugin_db(slug)) as db, db:
        db.execute("CREATE TABLE fixture_reminders (id TEXT PRIMARY KEY, due INTEGER NOT NULL)")
        db.executemany("INSERT INTO fixture_reminders VALUES (?, ?)", [("A1", 10), ("A2", 30)])

    def due(args):
        with closing(plugin_db(slug)) as db:
            rows = db.execute("SELECT id FROM fixture_reminders WHERE due <= ?", (args["now"],))
            return json.dumps([row[0] for row in rows])

    ctx = PluginContext(PluginManifest(name=slug), get_plugin_manager())
    ctx.register_tool(tool, slug, {
        "name": tool,
        "description": "List due fictional appointments.",
        "parameters": {"type": "object", "properties": {"now": {"type": "integer"}}, "required": ["now"]},
    }, due)
    created = json.loads(ctx.dispatch_tool(cronjob_tools.CRONJOB_SCHEMA["name"], {
        "action": "create", "schedule": "*/15 * * * *", "prompt": "Check fictional due appointments.",
        "name": "G2 reminder tick", "deliver": "local", "enabled_toolsets": [slug], "paused": True,
    }))
    assert created["success"], created
    job_id = created["job_id"]
    try:
        assert len(list_jobs(include_disabled=True)) == 1
        job = get_job(job_id)
        assert job["schedule_display"] == "*/15 * * * *"
        agent_factory = Mock()
        _construct_cron_agent(agent_factory, job, {}, _CronAgentSetup(runtime={}),
                              workdir=None, session_id="offline", session_db=None)
        enabled_toolsets = agent_factory.call_args.kwargs["enabled_toolsets"]
        assert enabled_toolsets == [slug]
        definitions = get_tool_definitions(enabled_toolsets, quiet_mode=True, skip_tool_search_assembly=True)
        assert tool in {definition["function"]["name"] for definition in definitions}
        result = handle_function_call(tool, {"now": 20}, enabled_toolsets=enabled_toolsets)
        assert json.loads(result) == ["A1"]
    finally:
        removed = json.loads(ctx.dispatch_tool(cronjob_tools.CRONJOB_SCHEMA["name"], {
            "action": "remove", "job_id": job_id,
        }))
        assert removed["success"], removed
    assert list_jobs(include_disabled=True) == []
