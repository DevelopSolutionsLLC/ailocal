#!/usr/bin/env python3
"""Exercise the installed LiteLLM adapter against a fake Ollama HTTP boundary."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import litellm
from harness import Suite, load_module

router = load_module('reasoning_router', Path('/app/config/hooks/reasoning_router.py'))
suite = Suite('Reasoning effort transport [APPROX: fake model backend]')
captured = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/api/chat':
            captured.append(body)
            result = {'model': body['model'], 'message': {'role': 'assistant', 'content': 'fixture'},
                      'done': True, 'done_reason': 'stop', 'prompt_eval_count': 1, 'eval_count': 1}
        else:
            result = {}
        wire = json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)

    def log_message(self, *args):
        pass


def send(data, responses=False):
    asyncio.run(router.ReasoningRouter().async_pre_call_hook(None, None, data, None))
    before = len(captured)
    if responses:
        litellm.responses(model='ollama_chat/fixture-model',
                          api_base=f'http://127.0.0.1:{server.server_port}',
                          input='fixture', **data)
    else:
        litellm.completion(model='ollama_chat/fixture-model',
                           api_base=f'http://127.0.0.1:{server.server_port}',
                           messages=[{'role': 'user', 'content': 'fixture'}], **data)
    suite.check(len(captured) == before + 1, 'request reached the Ollama chat boundary')
    return captured[-1]


server = HTTPServer(('127.0.0.1', 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    for effort, expected in [('none', False), ('minimal', 'low'), ('low', 'low'),
                             ('medium', 'medium'), ('high', 'high'), ('xhigh', 'xhigh')]:
        body = send({'reasoning_effort': effort})
        suite.check(body.get('think') == expected, f'{effort} survives SDK translation: {expected!r}')
    body = send({'reasoning_effort': 'xhigh', 'think': False})
    suite.check(body.get('think') is False, 'explicit native think:false outranks effort')
    body = send({'reasoning': {'effort': 'none'}}, responses=True)
    suite.check(body.get('think') is False, 'Responses effort none disables native thinking')
    body = send({'reasoning': {'effort': 'xhigh'}}, responses=True)
    suite.check(body.get('think') == 'xhigh', 'Responses effort xhigh survives SDK translation')
    body = send({'temperature': 0.7, 'top_p': 0.8, 'top_k': 20,
                 'min_p': 0.0, 'presence_penalty': 1.5, 'repeat_penalty': 1.0,
                 'allowed_openai_params': ['presence_penalty']})
    suite.check(body.get('options') == {
        'temperature': 0.7, 'top_p': 0.8, 'top_k': 20, 'min_p': 0.0,
        'presence_penalty': 1.5, 'repeat_penalty': 1.0},
        'Qwen sampling survives the installed SDK at the Ollama HTTP boundary')
    body = send({'stop': ['<|im_end|>', '<|im_start|>'], 'think': False})
    suite.check(body.get('options', {}).get('stop') == ['<|im_end|>', '<|im_start|>'],
                'profile chat boundaries survive the installed SDK at the Ollama HTTP boundary')
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
raise SystemExit(suite.report())
