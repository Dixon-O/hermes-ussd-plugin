"""Stand-in PluginContext that builds the real PlatformEntry (unknown kwargs raise TypeError)."""

from gateway.platform_registry import PlatformEntry


class FakeCtx:
    """Mirrors PluginContext.register_platform: builds a real PlatformEntry, so any kwarg the
    installed core does not know raises TypeError exactly as it would in production."""

    def __init__(self):
        self.entry = self.cli = self.commands = self.skills = None
        self.commands, self.skills = {}, {}

    def register_platform(self, name, label, adapter_factory, check_fn, validate_config=None,
                          required_env=None, install_hint="", **entry_kwargs):
        entry_kwargs.setdefault("plugin_name", "hermes-ussd")
        self.entry = PlatformEntry(name=name, label=label, adapter_factory=adapter_factory, check_fn=check_fn,
                                   validate_config=validate_config, required_env=required_env or [],
                                   install_hint=install_hint, source="plugin", **entry_kwargs)

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli = (name, setup_fn, handler_fn)

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = handler

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills[name] = path
