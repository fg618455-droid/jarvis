"""Application output paths honour an explicit root without remapping HOME."""
from pathlib import Path
import pytest
from jarvis.storage import data_directory, state_directory


def test_default_locations_remain_compatible(monkeypatch):
    monkeypatch.delenv('JARVIS_DATA_DIR', raising=False)
    assert data_directory() == Path.home()/'.local'/'share'/'jarvis'
    assert state_directory() == Path.home()/'.jarvis'


def test_relative_override_is_rejected_without_creating_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('JARVIS_DATA_DIR', 'relative')
    with pytest.raises(ValueError, match='absolute'):
        data_directory()
    assert not (tmp_path/'relative').exists()


def test_default_writers_use_owned_root(tmp_path, monkeypatch):
    root=tmp_path/'owned'
    home=Path.home()
    monkeypatch.setenv('JARVIS_DATA_DIR', str(root))
    monkeypatch.delenv('JARVIS_LLM_ROUTE_STATE_PATH', raising=False)
    monkeypatch.delenv('JARVIS_TTS_PROVIDER_STATE_PATH', raising=False)
    from jarvis.config import _default_db_path
    from jarvis.dictation.history import _default_history_path, DictationHistory
    from jarvis.reply.prompt_dump import _dump_dir
    from jarvis.output.tts import _get_piper_models_dir
    from jarvis.output.cloud_tts import default_tts_provider_state_path
    from jarvis.llm.route_state import default_state_path
    from jarvis.llm.probe import _save_catalogue
    from jarvis.utils.location import _cache_base_dir
    from desktop_app.paths import get_log_dir
    from desktop_app.app import get_lock_file_path
    paths=[Path(_default_db_path()), _default_history_path(), _dump_dir(),
           _get_piper_models_dir(), default_tts_provider_state_path(),
           default_state_path(), _save_catalogue([]), _cache_base_dir(),
           get_log_dir(), get_lock_file_path()]
    assert all(root in path.parents or path == root for path in paths)
    history=DictationHistory()
    history.add('synthetic storage acceptance')
    assert (root/'dictation_history.json').is_file()
    assert Path.home() == home


def test_explicit_state_path_keeps_precedence(tmp_path, monkeypatch):
    from jarvis.llm.route_state import default_state_path
    explicit=tmp_path/'explicit.json'
    monkeypatch.setenv('JARVIS_DATA_DIR', str(tmp_path/'owned'))
    monkeypatch.setenv('JARVIS_LLM_ROUTE_STATE_PATH', str(explicit))
    assert default_state_path() == explicit


def test_location_cache_paths_are_selected_before_application_import(tmp_path, monkeypatch):
    import json
    import os
    import subprocess
    import sys
    root=tmp_path/'owned'
    env={**os.environ, 'JARVIS_DATA_DIR':str(root), 'PYTHONDONTWRITEBYTECODE':'1'}
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[1] / 'src'), env.get('PYTHONPATH', '')]))
    code="from jarvis.utils.location import _LOCATION_CACHE_FILE,_CGNAT_CACHE_FILE; import json; print(json.dumps([str(_LOCATION_CACHE_FILE),str(_CGNAT_CACHE_FILE)]))"
    result=subprocess.run([sys.executable,'-B','-c',code],env=env,capture_output=True,text=True,timeout=20,check=True)
    assert json.loads(result.stdout.splitlines()[-1]) == [str(root/'location_cache.json'),str(root/'cgnat_cache.json')]
