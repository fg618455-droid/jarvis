"""Control centre API blueprints."""

from __future__ import annotations

from flask import Flask

from . import conversation, memory, security, settings, status, system, tools


BLUEPRINTS = (
    status.bp,
    memory.bp,
    conversation.bp,
    tools.bp,
    security.bp,
    system.bp,
    settings.bp,
)


def register_blueprints(app: Flask) -> None:
    """Attach every API blueprint to the control centre application."""
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)
