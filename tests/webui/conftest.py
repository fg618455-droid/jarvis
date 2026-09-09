"""Shared helpers for control centre tests.

The server refuses a request whose ``Host`` it does not recognise and a
write that arrives without ``X-Jarvis-UI``. Tests about the guards
themselves build their own client and send those headers deliberately;
tests about an endpoint's behaviour should not have to repeat them on every
call, so this client fills them in.

Every test here also runs against a config file of its own. What the control
centre shows is read from the configuration, and a suite reading the
developer's real one is reading a moving target: whichever crew endpoint,
MCP servers and LLM routes that machine happens to have configured decide
what the page renders and what background work the server starts behind it.
``JARVIS_CONFIG_PATH`` exists for exactly this, and nothing here is about
what one person configured.
"""

from __future__ import annotations

import os

import pytest
from flask.testing import FlaskClient
from werkzeug.datastructures import Headers

from jarvis.webui.server import WebUIConfig, create_app


@pytest.fixture(scope="session", autouse=True)
def _off_the_real_config(tmp_path_factory):
    """Point the whole suite at an empty config, before anything reads one.

    Session-scoped and autouse so it is in place before the module-scoped
    servers start: one of the things a server starts is the crew poller,
    which reads a configured endpoint over the network every ten seconds and
    publishes what it finds to every page that is open. Against a real crew
    URL that lands a real reading in the middle of a test that stubbed one,
    which is a failure that depends on the machine it ran on and on nothing
    the code did.
    """
    path = tmp_path_factory.mktemp("config") / "config.json"
    previous = os.environ.get("JARVIS_CONFIG_PATH")
    os.environ["JARVIS_CONFIG_PATH"] = str(path)
    yield path
    if previous is None:
        os.environ.pop("JARVIS_CONFIG_PATH", None)
    else:
        os.environ["JARVIS_CONFIG_PATH"] = previous


class ControlCentreClient(FlaskClient):
    """A client that looks like the control centre's own page."""

    def open(self, *args, **kwargs):
        headers = Headers(kwargs.get("headers") or {})
        headers.setdefault("Host", "127.0.0.1:5055")
        headers.setdefault("X-Jarvis-UI", "1")
        kwargs["headers"] = headers
        return super().open(*args, **kwargs)


def control_centre_client() -> ControlCentreClient:
    """A test client for the whole control centre application."""
    app = create_app(WebUIConfig(host="127.0.0.1", port=5055, token=""))
    app.config.update(TESTING=True)
    app.test_client_class = ControlCentreClient
    return app.test_client()


@pytest.fixture
def api_client() -> ControlCentreClient:
    return control_centre_client()
