"""Standalone synthetic model server. No Pliqra imports, external models or real billing."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
COUNTS = {'accepted': 0, 'completed': 0, 'disconnected': 0}
TEXT = 'Synthetic customer acceptance 🌍. This is a deterministic fixture, not a real language model.'


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def respond(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/v1/models':
            return self.respond(200, {'data': [{'id': 'synthetic-chat'}]})
        if self.path == '/fixture/counts':
            with LOCK:
                return self.respond(200, dict(COUNTS))
        return self.respond(404, {'error': 'Synthetic fixture route not found'})

    def do_POST(self):
        if self.path not in ('/v1/chat/completions', '/v1/responses', '/v1/messages'):
            return self.respond(404, {'error': 'Synthetic fixture route not found'})
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        expected = os.environ.get('SYNTHETIC_EXPECTED_AUTH')
        if expected and self.headers.get('Authorization') != expected:
            return self.respond(401, {'error': 'Synthetic credential mismatch'})
        with LOCK:
            COUNTS['accepted'] += 1
        tool = bool(body.get('tools'))
        args = '{"city":"Izmir"}'
        truncated = 'fixture:truncate' in json.dumps(body)
        slow = 'fixture:slow' in json.dumps(body)
        chat_usage = {'prompt_tokens': 16, 'completion_tokens': 24, 'total_tokens': 40}
        usage = {'input_tokens': 16, 'output_tokens': 24, 'total_tokens': 40}
        message = {'role': 'assistant', 'content': TEXT}
        if tool:
            message = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call_fixture', 'type': 'function', 'function': {'name': 'weather', 'arguments': args}}]}
        if self.path == '/v1/responses':
            item = {'id': 'fc_fixture', 'type': 'function_call', 'call_id': 'call_fixture', 'name': 'weather', 'arguments': args, 'status': 'completed'} if tool else {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': TEXT, 'annotations': []}]}
            result = {'id': 'resp_fixture', 'object': 'response', 'status': 'completed', 'output': [item], 'usage': usage}
            events = [{'type': 'response.created', 'response': {**result, 'status': 'in_progress', 'output': [], 'usage': None}},
                      {'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, **({'arguments': ''} if tool else {'content': []}), 'status': 'in_progress'}}]
            events += [{'type': 'response.function_call_arguments.delta' if tool else 'response.output_text.delta', 'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'delta': value} for value in ([args[:8], args[8:]] if tool else [TEXT[:12], TEXT[12:]])]
            events += [{'type': 'response.output_item.done', 'output_index': 0, 'item': item}, {'type': 'response.completed', 'response': result}]
        elif self.path == '/v1/messages':
            block = {'type': 'tool_use', 'id': 'call_fixture', 'name': 'weather', 'input': {'city': 'Izmir'}} if tool else {'type': 'text', 'text': TEXT}
            result = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'model': body['model'], 'content': [block], 'stop_reason': 'tool_use' if tool else 'end_turn', 'stop_sequence': None, 'usage': usage}
            events = [{'type': 'message_start', 'message': {**result, 'content': [], 'stop_reason': None, 'usage': {'input_tokens': 16, 'output_tokens': 0}}},
                      {'type': 'content_block_start', 'index': 0, 'content_block': {**block, **({'input': {}} if tool else {'text': ''})}}]
            events += [{'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': value} if tool else {'type': 'text_delta', 'text': value}} for value in ([args[:8], args[8:]] if tool else [TEXT[:12], TEXT[12:]])]
            events += [{'type': 'content_block_stop', 'index': 0}, {'type': 'message_delta', 'delta': {'stop_reason': result['stop_reason'], 'stop_sequence': None}, 'usage': {'output_tokens': 24}}, {'type': 'message_stop'}]
        else:
            result = {'id': 'fixture', 'object': 'chat.completion', 'model': body['model'], 'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls' if tool else 'stop'}], 'usage': chat_usage}
            if tool:
                events = [{'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'call_fixture', 'type': 'function', 'function': {'name': 'weather', 'arguments': args[:8]}}]}, 'finish_reason': None}]},
                          {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': args[8:]}}]}, 'finish_reason': 'tool_calls'}]}]
            else:
                events = [{'choices': [{'index': 0, 'delta': {'content': value}, 'finish_reason': None}]} for value in [TEXT[:12], TEXT[12:]]]
            events += [{'choices': [], 'usage': chat_usage}, '[DONE]']
        if not body.get('stream'):
            time.sleep(.005)
            self.respond(200, result)
            with LOCK:
                COUNTS['completed'] += 1
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        if truncated:
            events = events[:-1]
        try:
            for index, event in enumerate(events):
                if slow and index == 3:
                    time.sleep(3)
                if isinstance(event, str):
                    data = 'data: ' + event + '\n\n'
                else:
                    data = ('event: ' + event['type'] + '\n' if event.get('type') else '') + 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
                self.wfile.write(data.encode())
                self.wfile.flush()
                time.sleep(.03)
            with LOCK:
                COUNTS['completed'] += 1
        except (BrokenPipeError, ConnectionResetError):
            with LOCK:
                COUNTS['disconnected'] += 1
        self.close_connection = True


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8099), Handler).serve_forever()
