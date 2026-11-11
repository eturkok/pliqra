"""Keep streamed latency tied to the same completion boundary on every path."""
import httpx

from scripts.benchmark_gateway import measure


def test_stream_measurement_stops_at_completion_not_later_socket_close():
    class CompletionThenIdle(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"Synthetic"}}]}\n\n'
            yield b'data: [DONE]\n\n'
            raise AssertionError('Reading beyond the completed response changes the measurement')

        def close(self):
            self.closed = True

    body = CompletionThenIdle()
    with httpx.Client(base_url='https://fixture.test', transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=body))) as client:
        result = measure(client, '', ['fixture'], 1, 1, stream=True)
    assert body.closed
    assert result['requests'] == 1
    assert result['first_content']['p95_ms'] <= result['completion']['p95_ms']
