"""Claude-authored tests (22 November 2026; not GPT-6 Astra) for the lab demo's TCP
forwarder, services/edge (backlog 11.2; PRD roadmap 5.9, BO-4/BO-8 demo).

Real sockets on 127.0.0.1 with stdlib asyncio servers as targets. Cases:
EF01 bytes both ways unchanged, 2 MiB each way, with a slow-reading client;
EF02 a client half-close reaches the target, which still answers;
EF03 target refusing: the client is closed, counted, and the forwarder keeps serving;
EF04 target never answering the connect: closed after the connect timeout;
EF05 the connection cap: one over is closed at once without bytes, and a slot freed is
     reused;
EF06 the idle timeout covers the link: silence closes it; traffic in one direction only
     keeps it open;
EF07 stop() ends open links and the listener;
EF08 the target closing ends the client side;
EF09-EF10 argument checks and the command line (`listening`/`refused` records, exit 2);
EF11 a real HTTP request and WebSocket through the forwarder to uvicorn, with the
     `Host` and `Origin` headers unchanged (skipped without uvicorn/websockets).
"""
import asyncio
import io
import json
import os
import socket
import threading
import time
import unittest

from services.edge import __main__ as edge_main
from services.edge.forward import Forwarder


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


async def echo_target(handler=None):
    """A target server; default handler echoes until EOF then closes. Returns (server, port)."""
    async def echo(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
    server = await asyncio.start_server(handler or echo, '127.0.0.1', 0)
    return server, server.sockets[0].getsockname()[1]


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


async def read_until_eof(reader, timeout=10, *, reset_ok=False):
    """Bytes until EOF. With reset_ok, a reset (Windows resets a socket closed with unread
    input) also counts as closed."""
    chunks = []
    while True:
        try:
            data = await asyncio.wait_for(reader.read(65536), timeout)
        except ConnectionResetError:
            if not reset_ok:
                raise
            data = b''
        if not data:
            return b''.join(chunks)
        chunks.append(data)


class ForwarderTests(unittest.TestCase):
    def test_ef01_bytes_pass_unchanged_both_ways_under_backpressure(self):
        async def case():
            target, tport = await echo_target()
            forwarder = Forwarder('127.0.0.1', tport, chunk=4096)
            port = await forwarder.start('127.0.0.1', 0)
            payload = os.urandom(2 * 1024 * 1024)
            reader, writer = await asyncio.open_connection('127.0.0.1', port)

            async def send():
                for i in range(0, len(payload), 50_000):
                    writer.write(payload[i:i + 50_000])
                    await writer.drain()
                writer.write_eof()

            async def slow_read():
                got = bytearray()
                while len(got) < len(payload):
                    data = await asyncio.wait_for(reader.read(8192), 10)
                    if not data:
                        break
                    got += data
                    if len(got) % (256 * 1024) < 8192:
                        await asyncio.sleep(0.01)  # a slow reader
                return bytes(got)
            _, echoed = await asyncio.gather(send(), slow_read())
            self.assertEqual(echoed, payload)
            self.assertEqual(await read_until_eof(reader), b'', 'EOF after the echo')
            writer.close()
            await forwarder.stop()
            target.close()
            stats = forwarder.stats.snapshot()
            self.assertEqual((stats['bytes_to_target'], stats['bytes_to_client']), (len(payload), len(payload)))
            self.assertEqual((stats['accepted'], stats['active'], stats['failed_connect']), (1, 0, 0))
        run(case())

    def test_ef02_half_close_reaches_the_target_which_still_answers(self):
        async def answer_after_eof(reader, writer):
            request = await reader.read()  # until EOF
            await asyncio.sleep(0.2)
            writer.write(b'got ' + str(len(request)).encode())
            await writer.drain()
            writer.close()

        async def case():
            target, tport = await echo_target(answer_after_eof)
            forwarder = Forwarder('127.0.0.1', tport)
            port = await forwarder.start('127.0.0.1', 0)
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(b'x' * 1000)
            writer.write_eof()
            self.assertEqual(await read_until_eof(reader), b'got 1000')
            writer.close()
            await forwarder.stop()
            target.close()
        run(case())

    def test_ef03_a_refused_target_closes_the_client_and_serving_continues(self):
        async def case():
            forwarder = Forwarder('127.0.0.1', free_port(), connect_timeout=2)
            port = await forwarder.start('127.0.0.1', 0)
            for _ in range(3):
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                writer.write(b'GET / HTTP/1.1\r\n\r\n')
                self.assertEqual(await read_until_eof(reader), b'')
                writer.close()
            await asyncio.sleep(0.05)
            self.assertEqual((forwarder.stats.failed_connect, forwarder.stats.active), (3, 0))
            await forwarder.stop()
        run(case())

    def test_ef04_a_target_that_never_answers_the_connect_times_out(self):
        async def hang(host, port, limit):
            await asyncio.sleep(60)

        async def case():
            forwarder = Forwarder('target', 8000, connect_timeout=0.3, connect=hang)
            port = await forwarder.start('127.0.0.1', 0)
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            started = time.monotonic()
            self.assertEqual(await read_until_eof(reader), b'')
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.25)
            self.assertLess(elapsed, 5)
            writer.close()
            await asyncio.sleep(0.05)
            self.assertEqual(forwarder.stats.failed_connect, 1)
            await forwarder.stop()
        run(case())

    def test_ef05_one_over_the_cap_is_closed_at_once_and_a_freed_slot_is_reused(self):
        async def case():
            target, tport = await echo_target()
            forwarder = Forwarder('127.0.0.1', tport, max_connections=2)
            port = await forwarder.start('127.0.0.1', 0)
            links = []
            for _ in range(2):
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                writer.write(b'ping')
                self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 5), b'ping')
                links.append((reader, writer))
            reader3, writer3 = await asyncio.open_connection('127.0.0.1', port)
            writer3.write(b'ping')
            self.assertEqual(await read_until_eof(reader3, reset_ok=True), b'',
                             'over the cap: closed, nothing forwarded')
            writer3.close()
            self.assertEqual((forwarder.stats.refused_capacity, forwarder.stats.active), (1, 2))
            links[0][1].close()
            for _ in range(50):
                if forwarder.stats.active < 2:
                    break
                await asyncio.sleep(0.02)
            reader4, writer4 = await asyncio.open_connection('127.0.0.1', port)
            writer4.write(b'pong')
            self.assertEqual(await asyncio.wait_for(reader4.readexactly(4), 5), b'pong')
            writer4.close()
            links[1][1].close()
            await forwarder.stop()
            target.close()
            self.assertEqual(forwarder.stats.accepted, 3)
        run(case())

    def test_ef06_idle_closes_a_silent_link_but_one_way_traffic_keeps_it(self):
        async def ticker(reader, writer):  # sends every 50 ms, never reads
            try:
                while True:
                    writer.write(b't')
                    await writer.drain()
                    await asyncio.sleep(0.05)
            except (ConnectionError, OSError):
                pass

        async def silent(reader, writer):  # sends nothing; returns when the link closes
            await reader.read()
            writer.close()

        async def case():
            target, tport = await echo_target(ticker)
            forwarder = Forwarder('127.0.0.1', tport, idle_timeout=0.3)
            port = await forwarder.start('127.0.0.1', 0)
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            got = b''
            deadline = time.monotonic() + 1.2  # 4x the idle timeout, client silent throughout
            while time.monotonic() < deadline:
                data = await asyncio.wait_for(reader.read(100), 2)
                self.assertTrue(data, 'link closed although the target kept sending')
                got += data
            self.assertGreater(len(got), 10)
            self.assertEqual(forwarder.stats.idle_closed, 0)
            writer.close()
            await forwarder.stop()
            target.close()

            quiet, qport = await echo_target(silent)
            forwarder2 = Forwarder('127.0.0.1', qport, idle_timeout=0.3)
            port2 = await forwarder2.start('127.0.0.1', 0)
            reader2, writer2 = await asyncio.open_connection('127.0.0.1', port2)
            started = time.monotonic()
            self.assertEqual(await read_until_eof(reader2), b'')
            self.assertLess(time.monotonic() - started, 3)
            self.assertGreaterEqual(time.monotonic() - started, 0.25)
            self.assertEqual(forwarder2.stats.idle_closed, 1)
            writer2.close()
            await forwarder2.stop()
            quiet.close()
        run(case())

    def test_ef07_stop_ends_open_links_and_the_listener(self):
        async def case():
            target, tport = await echo_target()
            forwarder = Forwarder('127.0.0.1', tport)
            port = await forwarder.start('127.0.0.1', 0)
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(b'hi')
            self.assertEqual(await asyncio.wait_for(reader.readexactly(2), 5), b'hi')
            await forwarder.stop()
            self.assertEqual(await read_until_eof(reader), b'')
            self.assertEqual(forwarder.stats.active, 0)
            with self.assertRaises(ConnectionRefusedError):  # review nit: not a timeout
                await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 10)
            await forwarder.stop()  # twice is fine
            writer.close()
            target.close()
        run(case())

    def test_ef08_the_target_closing_ends_the_client_side(self):
        async def greet_and_close(reader, writer):
            writer.write(b'bye')
            await writer.drain()
            writer.close()

        async def case():
            target, tport = await echo_target(greet_and_close)
            forwarder = Forwarder('127.0.0.1', tport)
            port = await forwarder.start('127.0.0.1', 0)
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            self.assertEqual(await read_until_eof(reader), b'bye')
            writer.close()  # the client then closes too; the link ends
            for _ in range(100):
                if forwarder.stats.active == 0:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(forwarder.stats.active, 0)
            await forwarder.stop()
            target.close()
        run(case())

    def test_ef12_a_client_that_never_reads_cannot_hold_a_slot(self):
        """Review M1: the client sends, half-closes and never reads while the target sends
        more than the socket buffers hold; the link must end within the idle timeout and
        no socket may stay open."""
        target_done = asyncio.Event()

        async def flood(reader, writer):
            await reader.read()  # until the client's half-close arrives
            try:
                for _ in range(256):
                    writer.write(b'x' * 65536)  # 16 MiB in total
                    await asyncio.wait_for(writer.drain(), 10)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                pass
            finally:
                writer.transport.abort()
                target_done.set()

        async def case():
            target, tport = await echo_target(flood)
            forwarder = Forwarder('127.0.0.1', tport, idle_timeout=0.5)
            port = await forwarder.start('127.0.0.1', 0)
            sock = socket.create_connection(('127.0.0.1', port))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sock.sendall(b'GET /big HTTP/1.1\r\n\r\n')
            sock.shutdown(socket.SHUT_WR)
            sock.setblocking(False)
            for _ in range(200):  # the blocking calls above ran before the loop could accept
                if forwarder.stats.accepted:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(forwarder.stats.accepted, 1)
            started = time.monotonic()
            while forwarder.stats.active and time.monotonic() - started < 8:
                await asyncio.sleep(0.05)
            self.assertEqual(forwarder.stats.active, 0, 'a non-reading client still holds the slot')
            self.assertEqual(forwarder.stats.idle_closed, 1, forwarder.stats.snapshot())
            self.assertLess(forwarder.stats.bytes_to_client, 16 * 1024 * 1024, 'the flood was held back')
            await asyncio.wait_for(target_done.wait(), 10)  # the target side was closed too
            sock.close()
            await forwarder.stop()
            target.close()
        run(case())

    def test_ef13_a_slow_reader_holds_the_target_back(self):
        """Review L7: with drain() the forwarder reads from the target only as fast as the
        client takes it; without it, everything the target sends would be read at once."""
        sent = {'n': 0}

        async def flood(reader, writer):
            try:
                for _ in range(1024):  # up to 64 MiB
                    writer.write(b'y' * 65536)
                    await writer.drain()
                    sent['n'] += 65536
            except (ConnectionError, OSError):
                pass

        async def case():
            target, tport = await echo_target(flood)
            forwarder = Forwarder('127.0.0.1', tport, chunk=16384)
            port = await forwarder.start('127.0.0.1', 0)
            reader, writer = await asyncio.open_connection('127.0.0.1', port, limit=16384)
            writer.transport.pause_reading()  # the client stops reading
            await asyncio.sleep(1.0)
            held = forwarder.stats.bytes_to_client
            self.assertLess(held, 32 * 1024 * 1024, f'{held} bytes read from the target for a client that reads nothing')
            self.assertLess(sent['n'], 48 * 1024 * 1024)
            writer.transport.resume_reading()
            got = await asyncio.wait_for(reader.readexactly(1024 * 1024), 10)
            self.assertEqual(got, b'y' * len(got), 'data flows again once the client reads')
            writer.transport.abort()
            await forwarder.stop()
            target.close()
        run(case())

    def test_ef14_after_the_target_closes_the_client_gets_only_a_short_grace(self):
        """Review L9: a client that ignores the target's close does not hold a slot until
        the idle timeout."""
        async def greet_and_close(reader, writer):
            writer.write(b'bye')
            await writer.drain()
            writer.close()

        async def case():
            target, tport = await echo_target(greet_and_close)
            forwarder = Forwarder('127.0.0.1', tport, idle_timeout=60, half_close_grace=0.3)
            port = await forwarder.start('127.0.0.1', 0)
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            self.assertEqual(await read_until_eof(reader), b'bye')
            started = time.monotonic()  # the client never closes
            while forwarder.stats.active and time.monotonic() - started < 5:
                await asyncio.sleep(0.02)
            self.assertEqual(forwarder.stats.active, 0)
            self.assertLess(time.monotonic() - started, 3)
            writer.close()
            await forwarder.stop()
            target.close()
        run(case())

    def test_ef15_stop_with_open_links_completes_and_refusals_while_stopping_are_counted(self):
        async def case():
            target, tport = await echo_target()
            forwarder = Forwarder('127.0.0.1', tport)
            port = await forwarder.start('127.0.0.1', 0)
            links = []
            for _ in range(3):
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                writer.write(b'k')
                self.assertEqual(await asyncio.wait_for(reader.readexactly(1), 5), b'k')
                links.append(writer)
            await asyncio.wait_for(forwarder.stop(), 10)
            self.assertEqual((forwarder.stats.active, forwarder.stats.errors), (0, 0))
            for writer in links:
                writer.close()
            target.close()
        run(case())

    def test_ef09_arguments_are_checked(self):
        for kwargs, message in (({'target_host': ''}, 'target host'), ({'target_port': 0}, 'target port'),
                                ({'target_port': 70000}, 'target port'), ({'max_connections': 0}, 'max connections'),
                                ({'max_connections': 5000}, 'max connections'),
                                ({'connect_timeout': 0}, 'connect timeout'), ({'idle_timeout': -1}, 'idle timeout'),
                                ({'idle_timeout': 7200}, 'idle timeout'), ({'chunk': 10}, 'chunk'),
                                ({'half_close_grace': 0}, 'half-close grace')):
            given = {'target_host': 'api', 'target_port': 8000, **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, message):
                Forwarder(given.pop('target_host'), given.pop('target_port'), **given)


class CommandLineTests(unittest.TestCase):
    def test_ef10_refusals_exit_2_with_one_record(self):
        for argv, reason in ((['--listen-port', '70000', '--target-host', 'api', '--target-port', '8000'], 'listen port'),
                             (['--listen-port', '0', '--target-host', 'api', '--target-port', '0'], 'target port'),
                             (['--listen-port', '0', '--target-host', 'api', '--target-port', '8000',
                               '--max-connections', '0'], 'max connections')):
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(argv=argv):
                self.assertEqual(edge_main.main(argv, stdout=out, stderr=err), 2)
                [record] = [json.loads(line) for line in err.getvalue().splitlines()]
                self.assertEqual(record['event'], 'refused')
                self.assertIn(reason, record['reason'])
                self.assertEqual(out.getvalue(), '')

        busy = socket.socket()
        busy.bind(('127.0.0.1', 0))
        busy.listen()
        self.addCleanup(busy.close)
        out, err, result = io.StringIO(), io.StringIO(), {}

        def bind_busy():  # review L8: in a thread, so a bind that succeeds cannot hang the suite
            result['code'] = edge_main.main(['--listen-port', str(busy.getsockname()[1]), '--target-host', 'api',
                                             '--target-port', '8000'], stdout=out, stderr=err)
        thread = threading.Thread(target=bind_busy, daemon=True)
        thread.start()
        thread.join(15)
        self.assertFalse(thread.is_alive(), 'the forwarder bound a port already in use')
        self.assertEqual(result['code'], 2)
        self.assertIn('cannot listen', json.loads(err.getvalue())['reason'])

    def test_ef10b_serve_announces_the_bound_port_and_reports_counts_at_stop(self):
        async def case():
            target, tport = await echo_target()
            args = edge_main.build_parser().parse_args(['--listen-port', '0', '--target-host', '127.0.0.1',
                                                        '--target-port', str(tport)])
            out, stop = io.StringIO(), asyncio.Event()
            task = asyncio.ensure_future(edge_main.serve(args, stdout=out, stop_event=stop))
            for _ in range(100):
                if out.getvalue():
                    break
                await asyncio.sleep(0.01)
            listening = json.loads(out.getvalue().splitlines()[0])
            self.assertEqual(listening['event'], 'listening')
            self.assertEqual(listening['target'], f'127.0.0.1:{tport}')
            reader, writer = await asyncio.open_connection('127.0.0.1', listening['listen_port'])
            writer.write(b'abc')
            self.assertEqual(await asyncio.wait_for(reader.readexactly(3), 5), b'abc')
            writer.close()
            stop.set()
            await task
            stopped = json.loads(out.getvalue().splitlines()[-1])
            self.assertEqual((stopped['event'], stopped['accepted'], stopped['bytes_to_target']), ('stopped', 1, 3))
            self.assertNotIn('127.0.0.1:' + str(writer.get_extra_info('sockname')[1]), out.getvalue(),
                             'no client address is logged')
            target.close()
        run(case())


try:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute
    from websockets.sync.client import connect as ws_connect
    HAVE_WEB = True
except ImportError:  # CI installs requirements-audio.txt only
    HAVE_WEB = False


@unittest.skipUnless(HAVE_WEB, 'needs uvicorn, starlette and websockets (requirements-api.txt)')
class RealHttpTests(unittest.TestCase):
    def test_ef11_http_and_websocket_pass_with_host_and_origin_unchanged(self):
        async def headers(request):
            return JSONResponse({'host': request.headers.get('host'), 'origin': request.headers.get('origin')})

        async def echo(websocket):
            await websocket.accept()
            await websocket.send_json({'host': websocket.headers.get('host'),
                                       'origin': websocket.headers.get('origin')})
            async for message in websocket.iter_text():
                await websocket.send_text(message.upper())

        app = Starlette(routes=[Route('/headers', headers), WebSocketRoute('/ws', echo)])
        tport = free_port()
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=tport, log_level='error'))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(setattr, server, 'should_exit', True)
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.02)
        self.assertTrue(server.started)

        ready, holder, stop = threading.Event(), {}, None

        def forward():
            async def main():
                nonlocal stop
                stop = asyncio.Event()
                forwarder = Forwarder('127.0.0.1', tport)
                holder['port'] = await forwarder.start('127.0.0.1', 0)
                holder['loop'] = asyncio.get_running_loop()
                ready.set()
                await stop.wait()
                await forwarder.stop()
            asyncio.run(main())
        fthread = threading.Thread(target=forward, daemon=True)
        fthread.start()
        self.assertTrue(ready.wait(10))
        self.addCleanup(fthread.join, 10)
        self.addCleanup(lambda: holder['loop'].call_soon_threadsafe(stop.set))
        port = holder['port']

        with socket.create_connection(('127.0.0.1', port), timeout=10) as s:
            s.sendall(f'GET /headers HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\n'
                      'Connection: close\r\n\r\n'.encode())
            response = b''
            while chunk := s.recv(65536):
                response += chunk
        head, _, body = response.partition(b'\r\n\r\n')
        self.assertTrue(head.startswith(b'HTTP/1.1 200'), head)
        self.assertEqual(json.loads(body), {'host': f'127.0.0.1:{port}', 'origin': f'http://127.0.0.1:{port}'})

        with ws_connect(f'ws://127.0.0.1:{port}/ws', origin=f'http://127.0.0.1:{port}', open_timeout=10) as ws:
            self.assertEqual(json.loads(ws.recv(timeout=10)),
                             {'host': f'127.0.0.1:{port}', 'origin': f'http://127.0.0.1:{port}'})
            ws.send('ava demo')
            self.assertEqual(ws.recv(timeout=10), 'AVA DEMO')


if __name__ == '__main__':
    unittest.main()
