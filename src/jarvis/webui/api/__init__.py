"""Control centre API blueprints."""

from __future__ import annotations

from flask import Flask

from . import (
    briefing,
    conversation,
    crew,
    llm,
    mcp,
    memory,
    passive,
    security,
    settings,
    status,
    system,
    tools,
    visualizer,
    voice,
)


BLUEPRINTS = (
    status.bp,
    memory.bp,
    conversation.bp,
    passive.bp,
    tools.bp,
    mcp.bp,
    security.bp,
    system.bp,
    settings.bp,
    llm.bp,
    crew.bp,
    voice.bp,
    visualizer.bp,
    briefing.bp,
)


def register_blueprints(app: Flask) -> None:
    """Attach every API blueprint to the control centre application."""
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)
