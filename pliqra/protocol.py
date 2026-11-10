"""Pliqra Connection Protocol 1.0 and the frozen 0.1 compatibility envelope."""
from starlette._utils import get_route_path
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

PROTOCOL_VERSION = 'pliqra/1.0'
LEGACY_PROTOCOL_VERSION = 'pliqra/0.1-draft'
SPECIFICATION_VERSION = '1.0.0'
PROTOCOL_HEADER = 'Pliqra-Protocol'
SUPPORTED_PROTOCOLS = (PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION)


def version_metadata(selected):
    return {'protocol': selected, 'status': 'stable' if selected == PROTOCOL_VERSION else 'compatibility',
            'preferred_protocol': PROTOCOL_VERSION, 'supported_protocols': list(SUPPORTED_PROTOCOLS),
            'specification_version': SPECIFICATION_VERSION}


class ProtocolVersionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] == 'OPTIONS':
            return await self.app(scope, receive, send)
        path = get_route_path(scope)
        if not (path == '/.well-known/pliqra' or path.startswith('/protocol/v1/')):
            return await self.app(scope, receive, send)
        requested = Headers(scope=scope).getlist(PROTOCOL_HEADER)
        selected = requested[0] if requested else LEGACY_PROTOCOL_VERSION
        if len(requested) > 1 or selected not in SUPPORTED_PROTOCOLS:
            response = JSONResponse({'detail': {'code': 'unsupported_protocol', 'supported': list(SUPPORTED_PROTOCOLS)}},
                                    status_code=406, headers={'Cache-Control': 'no-store', 'Vary': PROTOCOL_HEADER})
            return await response(scope, receive, send)
        scope['pliqra.protocol'] = selected

        async def versioned_send(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                headers[PROTOCOL_HEADER] = selected
                headers.add_vary_header(PROTOCOL_HEADER)
                headers['Cache-Control'] = 'no-store'
            await send(message)
        await self.app(scope, receive, versioned_send)
