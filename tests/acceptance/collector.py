"""Bounded OTLP receiver for the owned synthetic lab; never a production collector."""
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

LOCK = threading.Lock()
SPANS = deque(maxlen=4096)
LEAK = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def reply(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        global LEAK
        length = int(self.headers.get('Content-Length', '0'))
        if self.path != '/v1/traces' or not 0 < length <= 4_000_000:
            return self.reply(400, {'error': 'Invalid synthetic export'})
        data = self.rfile.read(length)
        message = ExportTraceServiceRequest.FromString(data)
        with LOCK:
            LEAK |= any(marker in data for marker in (b'PRIVATE_OTLP_ACCEPTANCE', b'synthetic-provider-credential'))
            for resource in message.resource_spans:
                for scope in resource.scope_spans:
                    for span in scope.spans:
                        SPANS.append({'name': span.name, 'trace_id': span.trace_id.hex(),
                                      'request_id': next((a.value.string_value for a in span.attributes if a.key == 'pliqra.request.id'), None)})
        self.reply(200, {})

    def do_GET(self):
        if not self.path.startswith('/traces/'):
            return self.reply(404, {})
        trace_id = self.path.removeprefix('/traces/')
        with LOCK:
            self.reply(200, {'spans': [s for s in SPANS if s['trace_id'] == trace_id], 'content_leak': LEAK})


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 4318), Handler).serve_forever()
