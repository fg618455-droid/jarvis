"""Shared helpers for control centre tests.

The server refuses a request whose ``Host`` it does not recognise and a
write that arrives without ``X-Jarvis-UI``. Tests about the guards
themselves build their own client and send those headers deliberately;
tests about an endpoint's behaviour should not have to repeat them on every
call, so this client fills them in.
"""

from __future__ import annotations

import pytest
from flask.testing import FlaskClient
from werkzeug.datastructures import Headers

from jarvis.webui.server import WebUIConfig, create_app


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
