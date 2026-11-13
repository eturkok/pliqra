"""Opt-in, content-free tracing. No global provider or automatic instrumentation."""
import functools
import inspect
import os
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_current = ContextVar('pliqra_telemetry', default=None)


class Telemetry:
    def __init__(self, exporter=None):
        self.enabled = exporter is not None or os.getenv('PLIQRA_OTEL_ENABLED', '0') == '1'
        self.exporter = exporter
        self.provider = self.tracer = None

    def start(self):
        if not self.enabled or self.provider:
            return
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from . import __version__
        if self.exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            endpoint = os.getenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT')
            base = os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT')
            target = endpoint or ((base.rstrip('/') + '/v1/traces') if base else 'http://localhost:4318/v1/traces')
            parts = urlsplit(target)
            if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError('Configure a plain HTTP(S) OTLP traces endpoint; put collector credentials in OTLP headers')
            timeout = float(os.getenv('OTEL_EXPORTER_OTLP_TRACES_TIMEOUT', os.getenv('OTEL_EXPORTER_OTLP_TIMEOUT', '3')))
            if not 0.1 <= timeout <= 10:
                raise ValueError('OTLP export timeout must be between 0.1 and 10 seconds')
            self.exporter = OTLPSpanExporter(endpoint=target, timeout=timeout)
        # Explicit resources avoid machine names, process command lines and environment detection.
        self.provider = TracerProvider(resource=Resource({'service.name': 'pliqra', 'service.version': __version__}), shutdown_on_exit=False)
        self.provider.add_span_processor(BatchSpanProcessor(self.exporter, max_queue_size=2048, max_export_batch_size=128, schedule_delay_millis=1000, export_timeout_millis=10000))
        self.tracer = self.provider.get_tracer('pliqra', __version__)

    def close(self):
        if self.provider:
            self.provider.shutdown()
            self.provider = self.tracer = None

    def status(self):
        return {'enabled': self.enabled, 'protocol': 'http/protobuf', 'content_recorded': False,
                'queue_limit': 2048, 'correlation': 'pliqra.request.id', 'response_header': 'X-Pliqra-Trace-ID'}


@contextmanager
def span(name, **attributes):
    runtime = _current.get()
    if not runtime or not runtime.tracer:
        yield None
        return
    value = runtime.tracer.start_span(name, attributes=attributes)
    with trace.use_span(value, end_on_exit=True, record_exception=False, set_status_on_exception=False):
        try:
            yield value
        except BaseException:
            value.set_status(trace.StatusCode.ERROR)
            raise


def traced(name):
    def decorate(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def asynchronous(*args, **kwargs):
                with span(name):
                    return await fn(*args, **kwargs)
            return asynchronous
        @functools.wraps(fn)
        def synchronous(*args, **kwargs):
            with span(name):
                return fn(*args, **kwargs)
        return synchronous
    return decorate


def correlate(request_id):
    if _current.get():
        trace.get_current_span().set_attribute('pliqra.request.id', request_id)


class TracingMiddleware:
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope, receive, send):
        runtime = self.runtime()
        from starlette._utils import get_route_path
        path = get_route_path(scope) if scope['type'] == 'http' else ''
        # Child workspaces create their own request span using the shared provider.
        if scope['type'] != 'http' or not runtime.tracer or path.startswith(('/static/', '/org/')) or path in ('/', '/readyz', '/healthz', '/metrics'):
            return await self.app(scope, receive, send)
        carrier = {k.decode('latin1'): v.decode('latin1') for k, v in scope['headers'] if k.lower() == b'traceparent'}
        parent = TraceContextTextMapPropagator().extract(carrier)
        token = _current.set(runtime)
        root = runtime.tracer.start_span('pliqra.request', context=parent, kind=trace.SpanKind.SERVER,
                                         attributes={'http.request.method': scope.get('method', '')})
        try:
            with trace.use_span(root, end_on_exit=True, record_exception=False, set_status_on_exception=False):
                async def traced_send(message):
                    if message['type'] == 'http.response.start':
                        root.set_attribute('http.response.status_code', message['status'])
                        if message['status'] >= 400: root.set_status(trace.StatusCode.ERROR)
                        headers = [(k, v) for k, v in message.get('headers', []) if k.lower() != b'x-pliqra-trace-id']
                        headers.append((b'x-pliqra-trace-id', format(root.get_span_context().trace_id, '032x').encode()))
                        message = {**message, 'headers': headers}
                    await send(message)
                try:
                    await self.app(scope, receive, traced_send)
                except BaseException:
                    root.set_status(trace.StatusCode.ERROR)
                    raise
        finally:
            _current.reset(token)


class TracedStream(httpx.AsyncByteStream):
    def __init__(self, stream, value):
        self.stream, self.value, self.ended = stream, value, False
        self.complete = False

    def end(self, failed=False):
        if not self.ended:
            if failed: self.value.set_status(trace.StatusCode.ERROR)
            self.value.end()
            self.ended = True

    async def __aiter__(self):
        completed = False
        try:
            with trace.use_span(self.value, record_exception=False, set_status_on_exception=False):
                async for chunk in self.stream:
                    yield chunk
            completed = True
            self.complete = True
        finally:
            self.end(not completed)

    async def aclose(self):
        try:
            await self.stream.aclose()
        finally:
            self.end(not self.complete)


class TracingTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport):
        self.transport = transport

    async def handle_async_request(self, request):
        runtime = _current.get()
        if not runtime or not runtime.tracer:
            return await self.transport.handle_async_request(request)
        value = runtime.tracer.start_span('upstream.http', kind=trace.SpanKind.CLIENT,
                                           attributes={'http.request.method': request.method})
        try:
            with trace.use_span(value, record_exception=False, set_status_on_exception=False):
                carrier = {}
                TraceContextTextMapPropagator().inject(carrier)
                request.headers['traceparent'] = carrier['traceparent']
                response = await self.transport.handle_async_request(request)
                value.set_attribute('http.response.status_code', response.status_code)
                if response.status_code >= 400: value.set_status(trace.StatusCode.ERROR)
                if response.is_closed:
                    value.end()
                else:
                    response.stream = TracedStream(response.stream, value)
                return response
        except BaseException:
            value.set_status(trace.StatusCode.ERROR)
            value.end()
            raise

    async def aclose(self):
        await self.transport.aclose()
