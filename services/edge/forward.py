"""A bounded TCP forwarder for the lab demo (backlog 11.2; PRD roadmap 5.9 "demo script
and rehearsal", BO-4 and BO-8 shown on the dashboard).

The Compose stack keeps the API on internal networks with no route out (PRD: no
telemetry, no internet dependency). A browser on the host cannot reach it there. The
`demo` profile adds one container running this forwarder: it accepts connections on a
port that Compose publishes on the host's loopback address only, and copies bytes to
and from one fixed target (`api:8000`). The API itself stays internal, so its egress
proof is unchanged.

Deliberately a byte copier, not an HTTP proxy: the request, its `Host` and `Origin`
headers (the API's same-origin check compares them) and WebSocket frames pass through
unchanged. The target comes only from the command line, never from the traffic.

Bounds: at most `max_connections` at once (a connection over the cap is closed at once
and counted), a connect timeout, an idle timeout on the connection as a whole (bytes
read in either direction keep it open, so a quiet WebSocket that receives pings stays
up; a write stalled because the peer does not read is not activity), and reads of at
most `chunk` bytes with `drain()` after every write, so a slow side holds back the other
instead of filling memory. A client half-close is passed on (`write_eof`), so a client
that finishes sending still gets its answer; after the target closes, the client gets
`half_close_grace` seconds to close too. A link that does not end cleanly is aborted, so
no socket outlives it.
"""
import asyncio
from dataclasses import dataclass, field

DEFAULT_CHUNK = 64 * 1024


@dataclass
class ForwarderStats:
    accepted: int = 0
    refused_capacity: int = 0
    refused_stopping: int = 0
    failed_connect: int = 0
    errors: int = 0
    idle_closed: int = 0
    bytes_to_target: int = 0
    bytes_to_client: int = 0
    active: int = 0

    def snapshot(self):
        return dict(self.__dict__)


@dataclass
class _Link:
    last_activity: float
    tasks: list = field(default_factory=list)


class Forwarder:
    """Forward each accepted connection to (target_host, target_port).

    `connect` is injectable for tests (default `asyncio.open_connection`). Run with
    `await start(host, port)`; `await stop()` closes the listener and every open link.
    """

    def __init__(self, target_host, target_port, *, max_connections=32, connect_timeout=5.0,
                 idle_timeout=300.0, chunk=DEFAULT_CHUNK, half_close_grace=10.0, connect=None):
        if not target_host or not isinstance(target_host, str):
            raise ValueError('target host is required')
        if not 1 <= int(target_port) <= 65535:
            raise ValueError('target port must be 1-65535')
        if not 1 <= int(max_connections) <= 1024:
            raise ValueError('max connections must be 1-1024')
        if not 0 < float(connect_timeout) <= 60:
            raise ValueError('connect timeout must be more than 0 and at most 60 s')
        if not 0 < float(idle_timeout) <= 3600:
            raise ValueError('idle timeout must be more than 0 and at most 3600 s')
        if not 1024 <= int(chunk) <= 1024 * 1024:
            raise ValueError('chunk must be 1 KiB-1 MiB')
        self.target_host, self.target_port = target_host, int(target_port)
        self.max_connections = int(max_connections)
        self.connect_timeout, self.idle_timeout = float(connect_timeout), float(idle_timeout)
        self.chunk = int(chunk)
        if not 0 < float(half_close_grace) <= 60:
            raise ValueError('half-close grace must be more than 0 and at most 60 s')
        self.half_close_grace = float(half_close_grace)
        self._connect = connect or asyncio.open_connection
        self.stats = ForwarderStats()
        self._server = None
        self._handlers = set()
        self._stopping = False

    async def start(self, host, port):
        """Listen on (host, port); port 0 picks a free one. Returns the bound port."""
        self._server = await asyncio.start_server(self._accept, host, port, limit=self.chunk)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self):
        """Stop accepting, then end every open link (its tasks are cancelled and both
        sockets closed). Safe to call twice. Review L4: handlers are cancelled BEFORE
        `wait_closed()`, which from Python 3.12.1 waits for every open connection (a
        dashboard WebSocket kept alive by pings would otherwise block the stop)."""
        self._stopping = True
        if self._server is not None:
            self._server.close()
        handlers = list(self._handlers)
        for task in handlers:
            task.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5)
            except asyncio.TimeoutError:
                pass

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        if self._stopping:
            self.stats.refused_stopping += 1
            await _close(writer, abort=True)
            return
        if self.stats.active >= self.max_connections:
            self.stats.refused_capacity += 1
            await _close(writer, abort=True)
            return
        self.stats.accepted += 1
        self.stats.active += 1
        self._handlers.add(task)
        clean = False
        try:
            clean = await self._serve(reader, writer)
        except asyncio.CancelledError:
            pass
        finally:
            self.stats.active -= 1
            self._handlers.discard(task)
            await _close(writer, abort=not clean)

    async def _serve(self, client_reader, client_writer):
        """Run one link. Returns True when it ended cleanly (both directions reached EOF);
        otherwise the sockets are aborted, so nothing buffered keeps them open. Even a
        clean close gets only 2 s to flush (re-review R4): a client that has stopped
        reading at that moment may lose the last buffered bytes."""
        try:
            target_reader, target_writer = await asyncio.wait_for(
                self._connect(self.target_host, self.target_port, limit=self.chunk), self.connect_timeout)
        except (OSError, asyncio.TimeoutError):
            self.stats.failed_connect += 1
            return False
        loop = asyncio.get_running_loop()
        link = _Link(last_activity=loop.time())
        up = asyncio.ensure_future(self._pump(client_reader, target_writer, link, 'bytes_to_target'))
        down = asyncio.ensure_future(self._pump(target_reader, client_writer, link, 'bytes_to_client'))
        link.tasks = [up, down]
        clean = False
        try:
            done, _ = await asyncio.wait(link.tasks, return_when=asyncio.FIRST_COMPLETED)
            first_failed = any(task.exception() is not None for task in done)
            if not first_failed and up in done and not down.done():
                # The client half-closed: it may still wait for the answer, which the
                # download direction delivers under the idle timeout.
                await asyncio.wait([down])
            elif not first_failed and down in done and not up.done():
                # Review L9: the target closed. Give the client a short grace to finish
                # and close, instead of holding a slot until the idle timeout.
                await asyncio.wait([up], timeout=min(self.half_close_grace, self.idle_timeout))
            for task in link.tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*link.tasks, return_exceptions=True)
            if any(isinstance(result, _IdleTimeout) for result in results):
                self.stats.idle_closed += 1  # once per link, though both directions may time out
            unexpected = [r for r in results if isinstance(r, Exception)
                          and not isinstance(r, (_IdleTimeout, _LinkClosed, asyncio.CancelledError))]
            if unexpected:
                self.stats.errors += 1
            clean = all(result is None for result in results)
        finally:
            for task in link.tasks:
                task.cancel()
                # A direction that failed just before a stop cancelled this link: read its
                # exception so asyncio does not report it as never retrieved.
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
            await _close(target_writer, abort=not clean)
        return clean

    async def _pump(self, reader, writer, link, counter):
        loop = asyncio.get_running_loop()

        def remaining():
            return max(self.idle_timeout - (loop.time() - link.last_activity), 0.001)
        while True:
            try:
                data = await asyncio.wait_for(reader.read(self.chunk), remaining())
            except asyncio.TimeoutError:
                if loop.time() - link.last_activity < self.idle_timeout:
                    continue  # the other direction was active meanwhile
                raise _IdleTimeout() from None
            except (ConnectionError, OSError) as error:
                raise _LinkClosed() from error
            if not data:  # EOF: pass the half-close on, then this direction is done
                try:
                    if writer.can_write_eof():
                        writer.write_eof()
                except (ConnectionError, OSError, RuntimeError):
                    pass
                return
            link.last_activity = loop.time()
            setattr(self.stats, counter, getattr(self.stats, counter) + len(data))
            try:
                writer.write(data)
                # Backpressure: a slow peer holds this side back. Review M1: the wait is
                # under the idle timeout too, so a peer that never reads cannot hold the
                # link (a stalled write is not activity).
                while True:
                    try:
                        await asyncio.wait_for(writer.drain(), remaining())
                        break
                    except asyncio.TimeoutError:
                        if loop.time() - link.last_activity >= self.idle_timeout:
                            raise _IdleTimeout() from None
            except (ConnectionError, OSError) as error:
                raise _LinkClosed() from error


class _IdleTimeout(Exception):
    pass


class _LinkClosed(Exception):
    pass


async def _close(writer, *, abort=False):
    """Close a socket. A clean close waits up to 2 s to flush; after that, or when
    `abort` is set, the transport is aborted, so no socket outlives its link (review M1:
    otherwise unsent data kept it open while `active` had already gone down)."""
    try:
        if abort:
            writer.transport.abort()
            return
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 2)
    except asyncio.TimeoutError:
        writer.transport.abort()
    except (ConnectionError, OSError, RuntimeError):
        pass
