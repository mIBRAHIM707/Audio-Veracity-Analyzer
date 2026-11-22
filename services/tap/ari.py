"""Minimal Asterisk REST Interface (ARI) client for the passive tap (backlog 6.2;
PRD FR-T2; architecture §2.1). REST calls use the standard library (urllib, HTTP
basic auth, JSON); the event stream uses the `websockets` package
(`requirements-tap.txt`), imported only when `events()` is called. Asterisk 20 ARI.

Every REST call has a timeout and raises `AriError` (HTTP status, never the
credentials) for any transport, protocol or body fault, so callers handle one error
type. The event stream subscribes to ALL channel events (`subscribeAll=true`): the
tap learns about calls without the calls ever entering Stasis, so a tap that is dead,
slow or disconnected cannot hold a call (PRD FR-T4). Only the calls the tap needs are
wrapped; this is not a general ARI library.
"""
import base64
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_S = 5.0
CLEANUP_TIMEOUT_S = 2.0
MAX_BODY = 1 << 20


class AriError(RuntimeError):
    """`status`: the HTTP status, if any. `reason` (22 Nov, backlog 11.1): a short fixed
    token safe to log, `http_<status>`, the transport error's class name (for example
    `TimeoutError`, `ConnectionRefusedError`), `too_large` or `not_json`; never a URL,
    a body or the credentials."""
    def __init__(self, message, status=None, reason=None):
        super().__init__(message)
        self.status = status
        self.reason = reason if reason is not None else (f'http_{status}' if status is not None else None)


class AriClient:
    def __init__(self, base_url, user, password, app, *, timeout=TIMEOUT_S):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('base_url must be http(s)://host:port')
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment \
                or parsed.path not in ('', '/'):
            # Credentials in the URL would reach logs and the websocket URL (review M1).
            raise ValueError('base_url must be http(s)://host:port with no user, path, query or fragment')
        try:
            parsed.port
        except ValueError:
            raise ValueError('base_url has an invalid port') from None
        if not user or ':' in user:
            raise ValueError('user must be non-empty and contain no ":" (Basic authentication)')
        if not app or not app.replace('-', '').replace('_', '').isalnum():
            raise ValueError('app must be a simple name')
        self.base_url, self.app, self._timeout = base_url.rstrip('/'), app, timeout
        token = base64.b64encode(f'{user}:{password}'.encode('utf-8')).decode('ascii')
        self._auth = f'Basic {token}'

    def _request(self, method, path, params=None, *, timeout=None):
        what = f'ARI {method} {path.split("/")[0]}'
        query = '?' + urllib.parse.urlencode(params) if params else ''
        request = urllib.request.Request(f'{self.base_url}/ari/{path}{query}', method=method,
                                         headers={'Authorization': self._auth, 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:
                body = response.read(MAX_BODY + 1)
        except urllib.error.HTTPError as error:
            raise AriError(f'{what} failed with HTTP {error.code}', error.code) from None
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as error:
            # URLError wraps the socket error; its class is the useful part.
            cause = getattr(error, 'reason', None) if isinstance(error, urllib.error.URLError) else None
            name = type(cause).__name__ if isinstance(cause, BaseException) else type(error).__name__
            raise AriError(f'{what} failed: {name}', reason=name) from None
        if len(body) > MAX_BODY:
            raise AriError(f'{what} returned more than {MAX_BODY} bytes', reason='too_large')
        if not body.strip():
            return None
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            raise AriError(f'{what} returned a body that is not JSON', reason='not_json') from None

    # -- the calls the tap uses ------------------------------------------------------

    def snoop(self, channel_id, *, spy='in', app_args=''):
        """Snoop channel on any channel (the spied channel need not be in Stasis)."""
        return self._request('POST', f'channels/{_id(channel_id)}/snoop',
                             {'spy': spy, 'whisper': 'none', 'app': self.app, 'appArgs': app_args})

    def external_media(self, host_port, *, fmt='ulaw'):
        return self._request('POST', 'channels/externalMedia',
                             {'app': self.app, 'external_host': host_port, 'format': fmt,
                              'encapsulation': 'rtp', 'transport': 'udp', 'connection_type': 'client',
                              'direction': 'both'})

    def create_bridge(self, name=''):
        return self._request('POST', 'bridges', {'type': 'mixing', 'name': name})

    def add_to_bridge(self, bridge_id, channel_ids, *, timeout=None):
        return self._request('POST', f'bridges/{_id(bridge_id)}/addChannel',
                             {'channel': ','.join(_id(c) for c in channel_ids)}, timeout=timeout)

    def hangup(self, channel_id):
        return self._request('DELETE', f'channels/{_id(channel_id)}', timeout=CLEANUP_TIMEOUT_S)

    def destroy_bridge(self, bridge_id):
        return self._request('DELETE', f'bridges/{_id(bridge_id)}', timeout=CLEANUP_TIMEOUT_S)

    def get_variable(self, channel_id, name, *, timeout=None):
        """A channel variable's value, or None if it is not set (HTTP 404). `timeout`
        (seconds) overrides the client's for this read."""
        try:
            reply = self._request('GET', f'channels/{_id(channel_id)}/variable', {'variable': name}, timeout=timeout)
        except AriError as error:
            if error.status == 404:
                return None
            raise
        value = (reply or {}).get('value') if isinstance(reply, dict) else None
        return value or None

    def list_channels(self):
        return self._request('GET', 'channels')

    def list_bridges(self):
        return self._request('GET', 'bridges')

    def events_url(self):
        parsed = urllib.parse.urlsplit(self.base_url)
        scheme = 'wss' if parsed.scheme == 'https' else 'ws'
        return f'{scheme}://{parsed.netloc}/ari/events?' + urllib.parse.urlencode(
            {'app': self.app, 'subscribeAll': 'true'})

    def events(self, *, open_timeout=TIMEOUT_S):
        """The event WebSocket (websockets sync client). Use it as a context manager:
        `with ari.events() as connection:`; `connection.recv(timeout=...)` yields JSON
        text events and `close()` may be called from another thread."""
        from websockets.sync.client import connect
        return connect(self.events_url(), additional_headers={'Authorization': self._auth},
                       open_timeout=open_timeout, max_size=MAX_BODY)


def _id(value):
    """ARI IDs are opaque strings; refuse anything that could change the URL path."""
    if (not isinstance(value, str) or not value or len(value) > 128 or value in ('.', '..')
            or any(c in value for c in '/?#%\\ ')):
        raise ValueError('invalid ARI object id')
    return value
