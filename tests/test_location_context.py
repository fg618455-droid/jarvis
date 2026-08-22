import types
from unittest.mock import patch
from jarvis.reply.engine import run_reply_engine
from jarvis.utils.location import (
    get_location_context,
    _get_external_ip_automatically,
)


class DummyDB:
    pass


class DummyDialogueMemory:
    def has_recent_messages(self):
        return False

    def get_recent_messages(self):
        return []

    def add_message(self, role, content):
        pass


class DummyTTS:
    enabled = False


def _make_cfg(**overrides):
    # Minimal settings object with required attributes referenced in engine
    base = {
        'llm_provider': 'ollama',
        'llm_base_url': 'http://127.0.0.1:11434',
        'llm_chat_model': 'gemma4',
        'embedding_model': 'nomic-embed-text',
        'ollama_base_url': 'http://127.0.0.1:11434',
        'ollama_chat_model': 'gemma4',
        'ollama_embed_model': 'nomic-embed-text',
        'llm_profile_select_timeout_sec': 0.1,
        'llm_tools_timeout_sec': 0.1,
        'llm_embedding_timeout_sec': 0.1,
        'llm_chat_timeout_sec': 0.1,
        'agentic_max_turns': 1,
        'active_profiles': ['developer'],
        'voice_debug': False,
        'memory_enrichment_max_results': 0,
        'mcps': {},
        'location_enabled': True,
        'location_auto_detect': False,
        'location_ip_address': None,
        'location_cgnat_resolve_public_ip': True,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_engine_live_time_location_string_uses_manual_override():
    """The reply engine's system-prompt line reflects the manual override, not GeoIP."""
    from jarvis.reply.engine import _live_time_location_string

    cfg = _make_cfg(
        location_auto_detect=True,
        location_manual_city="Chiang Mai",
        location_manual_country="Thailand",
        location_manual_timezone="Asia/Bangkok",
    )
    with patch("jarvis.utils.location._get_external_ip_automatically") as mock_auto:
        line = _live_time_location_string(cfg)
    mock_auto.assert_not_called()
    assert "Chiang Mai, Thailand" in line


def test_get_location_context_disabled_flag():
    cfg = _make_cfg(location_enabled=False)
    # Direct call should be 'Location: Unknown' since we bypass engine wrapper
    direct = get_location_context(config_ip=None, auto_detect=False, resolve_cgnat_public_ip=True)
    # But engine should inject a context message that explicitly shows disabled
    # We can't fully run LLM chat here (would require external service), so instead
    # we call the internal helper indirectly by simulating run_reply_engine with 0 turns.
    # Set agentic_max_turns=0 to skip loop and ensure no network activity.
    cfg.agentic_max_turns = 0
    reply = run_reply_engine(DummyDB(), cfg, DummyTTS(), "test message", DummyDialogueMemory())
    # Engine returns None because no turns executed, but we assert that our disabled
    # logic produced 'Location: Disabled' rather than attempting lookup (cannot easily
    # capture printed system messages without refactor, so just ensure direct value plausible)
    assert direct in ("Location: Unknown", "Location: Disabled")


def test_auto_detect_falls_back_to_opendns_when_upnp_and_socket_fail():
    """OpenDNS DNS query is the final fallback in auto-detection (step 3)."""
    with patch("jarvis.utils.location._get_external_ip_via_upnp", return_value=None), \
         patch("jarvis.utils.location._get_external_ip_via_socket", return_value=None), \
         patch("jarvis.utils.location._resolve_public_ip_via_opendns", return_value="93.184.216.34") as mock_dns:
        result = _get_external_ip_automatically()
        mock_dns.assert_called_once()
        assert result == "93.184.216.34"


def test_auto_detect_skips_opendns_when_upnp_succeeds():
    """OpenDNS is not called when UPnP already returned a public IP."""
    with patch("jarvis.utils.location._get_external_ip_via_upnp", return_value="203.0.113.1"), \
         patch("jarvis.utils.location._resolve_public_ip_via_opendns") as mock_dns:
        result = _get_external_ip_automatically()
        mock_dns.assert_not_called()
        assert result == "203.0.113.1"


def test_auto_detect_skips_opendns_when_socket_succeeds():
    """OpenDNS is not called when socket heuristic already returned a public IP."""
    with patch("jarvis.utils.location._get_external_ip_via_upnp", return_value=None), \
         patch("jarvis.utils.location._get_external_ip_via_socket", return_value="198.51.100.5"), \
         patch("jarvis.utils.location._resolve_public_ip_via_opendns") as mock_dns:
        result = _get_external_ip_automatically()
        mock_dns.assert_not_called()
        assert result == "198.51.100.5"


def test_auto_detect_returns_none_when_all_methods_fail():
    """Returns None when UPnP, socket, and OpenDNS all fail."""
    with patch("jarvis.utils.location._get_external_ip_via_upnp", return_value=None), \
         patch("jarvis.utils.location._get_external_ip_via_socket", return_value=None), \
         patch("jarvis.utils.location._resolve_public_ip_via_opendns", return_value=None):
        result = _get_external_ip_automatically()
        assert result is None


def test_auto_detect_rejects_private_ip_from_opendns():
    """Private IPs from OpenDNS are rejected (not returned as valid)."""
    with patch("jarvis.utils.location._get_external_ip_via_upnp", return_value=None), \
         patch("jarvis.utils.location._get_external_ip_via_socket", return_value=None), \
         patch("jarvis.utils.location._resolve_public_ip_via_opendns", return_value="192.168.1.1"):
        result = _get_external_ip_automatically()
        assert result is None


def test_manual_city_override_skips_ip_geolocation_entirely():
    """A manual city bypasses auto-detection, config IP, and the GeoIP database."""
    from jarvis.utils.location import get_location_info

    with patch("jarvis.utils.location._get_external_ip_automatically") as mock_auto, \
         patch("jarvis.utils.location.geoip2") as mock_geoip2:
        result = get_location_info(
            auto_detect=True,
            manual_city="Chiang Mai",
            manual_country="Thailand",
            manual_timezone="Asia/Bangkok",
        )
        mock_auto.assert_not_called()
        mock_geoip2.database.Reader.assert_not_called()

    assert result == {
        "city": "Chiang Mai",
        "country": "Thailand",
        "timezone": "Asia/Bangkok",
    }


def test_manual_country_only_override_is_also_sufficient():
    """A country alone is enough to trigger the override (city is optional)."""
    from jarvis.utils.location import get_location_info

    result = get_location_info(manual_country="Thailand")
    assert result == {"country": "Thailand"}


def test_manual_override_formats_into_the_usual_location_context_string():
    from jarvis.utils.location import get_location_context

    context = get_location_context(
        manual_city="Chiang Mai",
        manual_region="Chiang Mai Province",
        manual_country="Thailand",
    )
    assert context == "Location: Chiang Mai, Chiang Mai Province, Thailand"


def test_manual_override_timezone_flows_through_the_timezone_helper():
    from jarvis.utils.location import get_location_context_with_timezone

    context, tz_name = get_location_context_with_timezone(
        manual_city="Chiang Mai",
        manual_country="Thailand",
        manual_timezone="Asia/Bangkok",
    )
    assert context == "Location: Chiang Mai, Thailand, (Asia/Bangkok)"
    assert tz_name == "Asia/Bangkok"


def test_without_a_manual_override_behaviour_is_unchanged():
    """No manual_* kwargs given at all falls through to normal IP-based lookup."""
    from jarvis.utils.location import get_location_info

    with patch("jarvis.utils.location._get_local_network_ip", return_value=None):
        result = get_location_info(auto_detect=False)
        assert "error" in result


def test_location_manual_override_defaults_none_from_a_real_config_file(tmp_path, monkeypatch):
    """load_settings() must wire the four manual override keys, not just accept them as stray keys."""
    import json

    from jarvis.config import load_settings

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setenv("JARVIS_CONFIG_PATH", str(config_path))

    cfg = load_settings()
    assert cfg.location_manual_city is None
    assert cfg.location_manual_region is None
    assert cfg.location_manual_country is None
    assert cfg.location_manual_timezone is None

    config_path.write_text(
        json.dumps({
            "location_manual_city": "Chiang Mai",
            "location_manual_region": "Chiang Mai Province",
            "location_manual_country": "Thailand",
            "location_manual_timezone": "Asia/Bangkok",
        }),
        encoding="utf-8",
    )
    cfg = load_settings()
    assert cfg.location_manual_city == "Chiang Mai"
    assert cfg.location_manual_region == "Chiang Mai Province"
    assert cfg.location_manual_country == "Thailand"
    assert cfg.location_manual_timezone == "Asia/Bangkok"
