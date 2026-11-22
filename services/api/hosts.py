"""Host-header allowlist for the analysis API (22 Nov 2026; backlog 11.2 review M4; PRD
§6.3; deploy/compose/README.md "Demo profile").

The API's same-origin check compares `Origin` with `Host`, and nothing checks the Host
value itself. On an address reachable from a browser (the lab demo publishes the API on
127.0.0.1), a web page can use DNS rebinding: it points its own name at 127.0.0.1, and
the browser then sends `Host: evil.example:8080` with a matching `Origin`, so both
checks pass and the page reads the API. With an allowlist, a request whose Host names
anything else is refused before it reaches the app: HTTP with 421, a WebSocket closed
with 1008 before it is accepted.

Hostnames only, compared without the port and case-insensitively (a trailing dot is
ignored), so `127.0.0.1:8080` through the demo forwarder and `127.0.0.1:8000` inside
the container both match `127.0.0.1`. A request with no Host or a malformed one is
refused. Pure ASGI, so it covers WebSocket handshakes, which Starlette's HTTP middleware
does not see. Lifespan events pass through unchanged.

Limit (re-review R3): ports are ignored, so a single-label name like `api` is also
accepted on the demo port. A rebinding attack under that name would need the victim's
resolver to answer for the bare name `api` (a hostile LAN DNS or a search-domain
hijack); list only names you need.
"""
import json
import re

_NAME = re.compile(r'^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$')
_IPV6 = re.compile(r'^[0-9a-f:.]{2,45}$')


def normalise_hostname(text):
    """A configured hostname in canonical form, or ValueError. `[::1]` and `::1` are
    the same name."""
    name = text.strip().lower().rstrip('.')
    if name.startswith('[') and name.endswith(']'):
        name = name[1:-1]
    if _NAME.match(name) or (':' in name and _IPV6.match(name)):
        return name
    raise ValueError(f'not a hostname: {text!r}')


def host_of(value):
    """The hostname of a Host header value (port removed), or None when malformed."""
    if not value or len(value) > 300:
        return None
    value = value.strip().lower()
    if value.startswith('['):
        end = value.find(']')
        rest = value[end + 1:] if end >= 0 else ''
        if end < 0 or (rest and not (re.fullmatch(r':\d{1,5}', rest) and int(rest[1:]) <= 65535)):
            return None
        name = value[1:end]
        return name if _IPV6.match(name) else None
    name, colon, port = value.partition(':')
    if colon and not (port.isdigit() and len(port) <= 5 and int(port) <= 65535):
        return None
    name = name.rstrip('.')
    return name if _NAME.match(name) else None


class HostAllowlist:
    """ASGI wrapper: only requests whose Host names one of `allowed` reach `app`.
    `refused` counts the requests turned away."""

    def __init__(self, app, allowed):
        names = frozenset(normalise_hostname(name) for name in allowed)
        if not names:
            raise ValueError('a host allowlist needs at least one hostname')
        self.app, self.allowed, self.refused = app, names, 0

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        hosts = [v.decode('latin-1') for k, v in scope.get('headers', ()) if k == b'host']
        name = host_of(hosts[0]) if len(hosts) == 1 else None
        if name is not None and name in self.allowed:
            return await self.app(scope, receive, send)
        self.refused += 1
        if scope['type'] == 'websocket':
            await receive()  # websocket.connect
            await send({'type': 'websocket.close', 'code': 1008})
            return
        body = json.dumps({'detail': 'Host not allowed'}).encode()
        await send({'type': 'http.response.start', 'status': 421,
                    'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode()),
                                (b'cache-control', b'no-store')]})
        await send({'type': 'http.response.body', 'body': body})
