import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from pliqra.main import create_app
from pliqra.telemetry import Telemetry, TracingTransport, _current, span
from tests.test_app import connect, call, Fragments
from tests.test_fabric import gateway, rail, bound_key, invoke


@pytest.fixture
def traced_workspace(tmp_path):
    exporter = InMemorySpanExporter()
    runtime = Telemetry(exporter)
    runtime.start()
    captured = []
    def upstream(request):
        captured.append(request)
        if request.url.path.endswith('/models'): return httpx.Response(200,json={'data':[{'id':'quartz-mini'}]})
        body = json.loads(request.content)
        if body.get('stream'):
            return httpx.Response(200,stream=Fragments('data: {"choices":[{"delta":{"content":"secret-output"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\ndata: [DONE]\n\n'))
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'secret-output'},'finish_reason':'stop'}],'usage':{'prompt_tokens':1,'completion_tokens':1}})
    app = create_app(tmp_path,telemetry_runtime=runtime)
    app.state.upstream_transport = httpx.MockTransport(upstream)
    with TestClient(app) as client:
        setup = client.post('/api/v1/setup',json={'name':'Owner','email':'owner@example.test','password':'long-local-password'})
        client.headers['X-Pliqra-CSRF'] = setup.json()['csrf']
        yield client, app, captured, runtime, exporter
    runtime.close()


def test_trace_parent_correlation_privacy_and_guardrail_spans(traced_workspace):
    client,app,captured,runtime,exporter = traced_workspace
    service = connect(client)['services'][0]
    guard = rail(client)
    profile = gateway(client,[service['id']],guardrails=[{'guardrail_id':guard['id'],'stages':['input']}])
    _, headers = bound_key(client,profile)
    parent = 'a'*32
    response = invoke(client,service['name'],headers={**headers,'traceparent':'00-'+parent+'-'+'b'*16+'-01'},stream=True,max_tokens=128)
    assert response.status_code == 200, response.text
    assert '[DONE]' in response.text
    assert response.headers['X-Pliqra-Trace-ID'] == parent
    assert captured[-1].headers['traceparent'].split('-')[1] == parent
    runtime.provider.force_flush()
    spans = [s for s in exporter.get_finished_spans() if format(s.context.trace_id,'032x') == parent]
    assert {'pliqra.request','database.query','secret.resolve','guardrail.check','upstream.http'} <= {s.name for s in spans}
    roots = [s for s in spans if s.name == 'pliqra.request']
    assert len(roots) == 1 and roots[0].parent.span_id == int('b'*16,16)
    event_id = client.get('/api/v1/events').json()['items'][0]['id']
    assert roots[0].attributes['pliqra.request.id'] == event_id
    assert max(s.end_time for s in spans) == roots[0].end_time
    content = json.dumps([{'name':s.name,'attrs':dict(s.attributes),'events':[str(e) for e in s.events]} for s in spans])
    for forbidden in ['Private prompt','secret-output','provider-secret-value','owner@example.test','SELECT','Authorization']:
        assert forbidden not in content


def test_outbound_span_ends_after_stream_or_cancellation():
    async def check():
        exporter = InMemorySpanExporter(); runtime = Telemetry(exporter); runtime.start()
        token = _current.set(runtime)
        class Slow(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'first'
                yield b'last'
        transport = TracingTransport(httpx.MockTransport(lambda r: httpx.Response(200,stream=Slow())))
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                with span('parent'):
                    response = await client.send(client.build_request('GET','https://model.test'),stream=True)
                    runtime.provider.force_flush()
                    assert not any(s.name == 'upstream.http' for s in exporter.get_finished_spans())
                    await response.aread(); await response.aclose()
            runtime.provider.force_flush()
            assert len([s for s in exporter.get_finished_spans() if s.name == 'upstream.http']) == 1
        finally:
            _current.reset(token); runtime.close()
    asyncio.run(check())


def test_disabled_tracing_does_not_propagate_headers(tmp_path):
    captured = []
    app = create_app(tmp_path)
    app.state.upstream_transport = httpx.MockTransport(lambda r: captured.append(r) or httpx.Response(200,json={'data':[{'id':'model'}]}))
    with TestClient(app) as client:
        setup = client.post('/api/v1/setup',json={'name':'Owner','email':'owner@example.test','password':'long-local-password'})
        client.headers['X-Pliqra-CSRF'] = setup.json()['csrf']
        response = client.post('/api/v1/connections',json={'name':'Model','endpoint':'https://model.test'})
        assert response.status_code == 201 and 'x-pliqra-trace-id' not in response.headers
        assert all('traceparent' not in r.headers for r in captured)
        status = client.get('/api/v1/diagnostics/runtime').json()
        assert status['telemetry']['enabled'] is False and status['login_protection']['shared']


def test_actual_otlp_http_protobuf_export(monkeypatch):
    received = []
    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):
            data = self.rfile.read(int(self.headers['Content-Length']))
            received.append((self.path,self.headers.get('Content-Type'),ExportTraceServiceRequest.FromString(data)))
            self.send_response(200); self.end_headers()
        def log_message(self,*args): pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Collector)
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('PLIQRA_OTEL_ENABLED','1')
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT',f'http://127.0.0.1:{server.server_port}/v1/traces')
    runtime = Telemetry(); runtime.start(); token = _current.set(runtime)
    try:
        with span('integration.checked'): pass
        assert runtime.provider.force_flush(timeout_millis=5000)
        assert received[0][:2] == ('/v1/traces','application/x-protobuf')
        assert received[0][2].resource_spans[0].scope_spans[0].spans[0].name == 'integration.checked'
    finally:
        _current.reset(token);runtime.close();server.shutdown();server.server_close();thread.join()


def test_invalid_parent_and_unavailable_collector_do_not_block_inference(traced_workspace):
    client,app,captured,runtime,exporter = traced_workspace
    service = connect(client)['services'][0]
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult, BatchSpanProcessor
    class Unavailable(SpanExporter):
        def export(self,spans): return SpanExportResult.FAILURE
        def shutdown(self): pass
    runtime.provider.add_span_processor(BatchSpanProcessor(Unavailable(),max_queue_size=16,max_export_batch_size=8))
    response = call(client,service['name'],max_tokens=32)
    assert response.status_code == 200
    # Invalid parents are discarded; baggage and tracestate are not forwarded.
    response = client.post('/v1/chat/completions',headers={'traceparent':'invalid','baggage':'private=value','tracestate':'private=value'},json={'model':service['name'],'messages':[{'role':'user','content':'private'}],'max_tokens':32})
    assert response.status_code == 200
    assert len(response.headers['X-Pliqra-Trace-ID']) == 32
    assert captured[-1].headers['traceparent'].split('-')[1] == response.headers['X-Pliqra-Trace-ID']
    assert 'baggage' not in captured[-1].headers and 'tracestate' not in captured[-1].headers
    assert runtime.provider.force_flush(timeout_millis=5000)


def test_closing_an_unconsumed_upstream_stream_records_one_error():
    async def check():
        exporter=InMemorySpanExporter();runtime=Telemetry(exporter);runtime.start();token=_current.set(runtime)
        try:
            transport=TracingTransport(httpx.MockTransport(lambda r:httpx.Response(200,stream=Fragments('partial'))))
            async with httpx.AsyncClient(transport=transport) as client:
                with span('parent'):
                    response=await client.send(client.build_request('GET','https://model.test'),stream=True)
                    await response.aclose();await response.aclose()
            runtime.provider.force_flush()
            spans=[s for s in exporter.get_finished_spans() if s.name=='upstream.http']
            assert len(spans)==1 and spans[0].status.status_code.name=='ERROR'
        finally:_current.reset(token);runtime.close()
    asyncio.run(check())


def test_preconsumed_transport_response_does_not_leave_an_open_span():
    async def check():
        exporter=InMemorySpanExporter();runtime=Telemetry(exporter);runtime.start();token=_current.set(runtime)
        try:
            transport=TracingTransport(httpx.MockTransport(lambda r:httpx.Response(200,json={'ok':True})))
            async with httpx.AsyncClient(transport=transport) as client:
                with span('parent'):
                    assert (await client.get('https://model.test')).json()=={'ok':True}
            runtime.provider.force_flush()
            assert len([s for s in exporter.get_finished_spans() if s.name=='upstream.http'])==1
        finally:_current.reset(token);runtime.close()
    asyncio.run(check())
