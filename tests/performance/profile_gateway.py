"""Opt-in diagnostic factory for the owned benchmark lab, never a production entrypoint."""
from contextvars import ContextVar
import time
import psycopg

CURRENT = ContextVar('benchmark_sql', default=None)


class TracedConnection(psycopg.Connection):
    def execute(self, query, params=None, **kwargs):
        started = time.perf_counter()
        try:
            return super().execute(query, params, **kwargs)
        finally:
            trace = CURRENT.get()
            if trace is not None:
                sql = str(query).lower()
                bucket = ('clock' if 'clock_timestamp' in sql else 'lock' if 'pg_advisory' in sql
                          else 'settings' if 'from settings' in sql else 'other')
                trace['calls'] += 1
                trace['ms'] += (time.perf_counter() - started) * 1000
                trace[bucket] += 1


def create_app():
    # A separate diagnostic container runs this factory; ordinary images are untouched.
    psycopg.connect = TracedConnection.connect
    from pliqra.main import create_app as original
    app = original()

    @app.middleware('http')
    async def timing(request, call_next):
        trace = {'calls': 0, 'ms': 0., 'clock': 0, 'lock': 0, 'settings': 0, 'other': 0}
        token = CURRENT.set(trace)
        try:
            response = await call_next(request)
            for key, value in trace.items():
                response.headers['X-Benchmark-DB-' + key] = str(round(value, 3))
            return response
        finally:
            CURRENT.reset(token)

    return app
