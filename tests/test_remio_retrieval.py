import json
from subprocess import CompletedProcess

from jarvis.memory.remio import RemioAdapter


def test_remio_reads_only_the_best_limited_search_hits():
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "search_notes":
            return CompletedProcess(command, 0, json.dumps({"ok": True, "data": {"results": [
                {"noteId": "one", "title": "One", "preview": "first"},
                {"noteId": "two", "title": "Two", "preview": "second"},
            ]}}), "")
        return CompletedProcess(command, 0, json.dumps({"ok": True, "data": {
            "content": "A bounded note body",
        }}), "")

    result = RemioAdapter(run=run, max_results=1, read_chars=100).search("project alpha")

    assert result[0].provenance.kind == "remio"
    assert result[0].provenance.identifier == "One"
    assert len(commands) == 2
    assert commands[0][:2] == ["remio", "search_notes"]


def test_remio_failure_is_an_empty_memory_source():
    def run(command, **kwargs):
        return CompletedProcess(command, 1, "", "not running")

    assert RemioAdapter(run=run).search("project alpha") == []
