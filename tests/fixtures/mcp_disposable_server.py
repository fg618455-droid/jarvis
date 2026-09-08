"""Minimal real stdio MCP peer for transport lifecycle acceptance."""
import json
import os
from pathlib import Path
import sys

marker = Path(sys.argv[1])
with marker.open('a', encoding='utf-8') as stream:
    stream.write(str(os.getpid()) + '\n')
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request['method']
    if method == 'initialize':
        result = {'protocolVersion': request['params']['protocolVersion'],
                  'capabilities': {'tools': {}}, 'serverInfo': {'name': 'disposable', 'version': '1.0.0'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': name, 'description': 'Synthetic lifecycle check',
                            'inputSchema': {'type': 'object', 'properties': {}}}
                           for name in ['echo', 'tool_error', 'crash_once', 'crash_always']]}
    elif method == 'tools/call':
        name = request['params']['name']
        if name == 'crash_always':
            os._exit(7)
        if name == 'crash_once' and not marker.with_suffix('.crashed').exists():
            marker.with_suffix('.crashed').write_text('once', encoding='utf-8')
            os._exit(7)
        result = {'content': [{'type': 'text', 'text': 'synthetic'}], 'isError': name == 'tool_error'}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
