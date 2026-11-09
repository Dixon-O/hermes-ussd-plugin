"""hermes-ussd: talk to Hermes from any phone, over USSD and SMS, via Africa's Talking.

Import-light on purpose: Hermes loads this module during plugin discovery in every process, so the
adapter (aiohttp, httpx, sqlite) is imported only inside ``register``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

__version__ = "0.1.0"

_HERE = Path(__file__).resolve().parent


def register(ctx) -> None:
    """Plugin entry point, called by the Hermes plugin system."""
    from gateway.platform_registry import PlatformEntry

    from .adapter import (
        PLATFORM_HINT,
        ATAdapter,
        check_requirements,
        env_enablement,
        is_connected,
        validate_config,
    )
    from .sender import parse_target, standalone_send, validate_target

    kwargs = dict(
        name="africastalking",
        label="USSD & SMS (Africa's Talking)",
        adapter_factory=ATAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["AT_USERNAME", "AT_API_KEY", "AT_WEBHOOK_SECRET"],
        install_hint="pip install 'aiohttp>=3.9,<4' (Hermes' [messaging] extra includes it)",
        is_connected=is_connected,
        env_enablement_fn=env_enablement,
        cron_deliver_env_var="AT_HOME_CHANNEL",
        standalone_sender_fn=standalone_send,
        parse_target_ref_fn=parse_target,
        validate_target_ref_fn=validate_target,
        allowed_users_env="AT_ALLOWED_USERS",
        allow_all_env="AT_ALLOW_ALL_USERS",
        max_message_length=459,
        pii_safe=True,
        emoji="📟",
        allow_update_command=False,  # nobody runs /update from a text message
        platform_hint=PLATFORM_HINT,
        dashboard_description="Reach Hermes from any phone: ask over USSD, get the answer by SMS.",
    )
    # Feature-detect fields newer than the oldest supported core. Never set trusted_inbound: this is a
    # chat platform, so the gateway's allowlist must stay in force.
    known = {f.name for f in dataclasses.fields(PlatformEntry)}
    for key, value in (("allowlist_suppresses_pairing", True), ("display_tier", "minimal")):
        if key in known:
            kwargs[key] = value
    kwargs = {k: v for k, v in kwargs.items() if k in known or k in (
        "name", "label", "adapter_factory", "check_fn", "validate_config", "required_env", "install_hint")}
    ctx.register_platform(**kwargs)

    from .cli import register_cli, ussd_command

    register_cli(ctx)
    ctx.register_command("ussd", ussd_command, description="Show hermes-ussd status", args_hint="status")

    skill = _HERE / "skills" / "sms-replies" / "SKILL.md"
    if skill.exists():
        ctx.register_skill("sms-replies", skill, description="Write answers that read well over SMS")
