"""Real mitmproxy selection and executable P1 cancellation stop-gate evidence.

No production socket/config/process is touched. Counterexamples use fake local
transport objects, not unreachable public endpoints or timing-sensitive networks.
"""
import asyncio
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import patch

from mitmproxy import connection, options
from mitmproxy.proxy import commands, events
from mitmproxy.proxy.context import Context
from mitmproxy.proxy.layers.http import (
    HttpLayer, HTTPMode, GetHttpConnection, GetHttpConnectionCompleted,
)
from mitmproxy.proxy.server import ConnectionHandler, ConnectionIO
from mitmproxy.proxy import server_hooks
from engine import mitm_transport_adapter as adapter
from engine.connection_policy import ConnectionGovernance
from tests.test_connection_policy import flow_for


class Stream:
    def __init__(self, flow):
        self.flow = flow
        self.received = []

    def handle_event(self, event):
        self.received.append(event)
        yield from ()


@unittest.skipUnless(adapter.transport_capabilities()["observation"], "requires mitmproxy 12.2.3")
class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.g = ConnectionGovernance()
        self.original_get = HttpLayer.get_connection
        self.original_child = HttpLayer.event_to_child
        self.assertTrue(self.g.running())
        client = connection.Client(peername=("127.0.0.1", 1), sockname=("127.0.0.1", 2))
        self.context = Context(client, options.Options())
        self.layer = HttpLayer(self.context, HTTPMode.regular)

    def tearDown(self):
        self.g.done()

    def request(self, conn):
        flow = flow_for(conn)
        self.g.request_started(flow)
        stream = Stream(flow)
        event = GetHttpConnection(conn.address, conn.tls, conn.via, conn.transport_protocol)
        self.layer.command_sources[event] = stream
        return flow, stream, event

    def test_pool_actual_selection_not_request_server(self):
        conn = connection.Server(address=("private", 80))
        conn.state = connection.ConnectionState.OPEN
        self.layer.connections[conn] = object()
        flow, stream, event = self.request(connection.Server(address=conn.address))
        before = flow.request.raw_content
        self.assertEqual(list(self.layer.get_connection(event)), [])
        self.assertIs(stream.received[0].reply[0], conn)
        self.assertTrue(self.g.snapshot(flow)["server_conn_id"].endswith(str(conn.id)))
        self.assertEqual(flow.request.raw_content, before)
        self.g.response_complete(flow)
        second, _, command = self.request(conn)
        list(self.layer.get_connection(command))
        self.assertTrue(self.g.snapshot(second)["reused"])

    def test_connected_context_even_reuse_false(self):
        conn = self.context.server
        conn.address = ("private", 80)
        conn.state = connection.ConnectionState.OPEN
        flow, stream, event = self.request(conn)
        list(self.layer.get_connection(event, reuse=False))
        self.assertIs(stream.received[0].reply[0], conn)
        self.assertIn(conn, self.layer.connections)
        self.assertTrue(self.g.snapshot(flow)["server_conn_id"].endswith(str(conn.id)))
        # This executable counterexample is why reuse=False is not advertised
        # as a strict isolation policy, even with HTTP/1.1.

    def test_pending_shared_waiter_observed_without_commands(self):
        conn = connection.Server(address=("private", 80))
        self.layer.connections[conn] = object()
        self.layer.waiting_for_establishment[conn] = []
        flows = []
        for _ in range(2):
            flow, _, event = self.request(conn)
            flows.append(flow)
            self.assertEqual(list(self.layer.get_connection(event)), [])
            self.assertIn(event, self.layer.waiting_for_establishment[conn])
            self.assertTrue(self.g.snapshot(flow)["server_conn_id"].endswith(str(conn.id)))
        self.assertEqual(len(self.layer.waiting_for_establishment[conn]), 2)
        self.g.server_connect(SimpleNamespace(server=conn))
        self.g.server_connect_error(SimpleNamespace(server=conn))
        for flow in flows:
            self.assertEqual(self.g.snapshot(flow)["reason"], "connect_failed")

    def test_equal_connection_specs_do_not_confuse_pending_identity(self):
        first, _, event1 = self.request(connection.Server(address=("private", 80)))
        first_commands = list(self.layer.get_connection(event1, reuse=False))
        second, _, event2 = self.request(connection.Server(address=("private", 80)))
        second_commands = list(self.layer.get_connection(event2, reuse=False))
        self.assertEqual(event1, event2)  # dataclass equality is NOT waiter identity.
        opens = [[c.connection for c in output if isinstance(c, commands.OpenConnection)][0]
                 for output in (first_commands, second_commands)]
        self.assertIsNot(opens[0], opens[1])
        for flow, conn in zip((first, second), opens):
            self.assertTrue(self.g.snapshot(flow)["server_conn_id"].endswith(str(conn.id)))

    def test_new_connection_pending_before_open_hook(self):
        flow, _, event = self.request(connection.Server(address=("private", 80)))
        output = list(self.layer.get_connection(event))
        opens = [command for command in output if isinstance(command, commands.OpenConnection)]
        self.assertEqual(len(opens), 1)
        selected = opens[0].connection
        self.assertTrue(self.g.snapshot(flow)["server_conn_id"].endswith(str(selected.id)))

    def test_multiobserver_idempotence_and_uninstall(self):
        wrapped = HttpLayer.get_connection
        self.g.running()
        other = ConnectionGovernance()
        other.running()
        self.assertIs(HttpLayer.get_connection, wrapped)
        self.g.done()
        self.assertIs(HttpLayer.get_connection, wrapped)
        other.done()
        other.done()
        self.assertIs(HttpLayer.get_connection, self.original_get)
        self.assertIs(HttpLayer.event_to_child, self.original_child)

    def test_observer_failure_cannot_change_command_stream(self):
        conn = connection.Server(address=("private", 80))
        conn.state = connection.ConnectionState.OPEN
        self.layer.connections[conn] = object()
        _, stream, event = self.request(conn)
        with patch.object(self.g, "connection_selected", side_effect=RuntimeError("diagnostics")):
            self.assertEqual(list(self.layer.get_connection(event)), [])
        self.assertEqual(self.g.observation_errors, 1)
        self.assertIs(stream.received[0].reply[0], conn)

    def test_h2_native_selection_unchanged(self):
        conn = connection.Server(address=("private", 443))
        conn.state = connection.ConnectionState.OPEN
        conn.tls = True
        conn.alpn = b"h2"
        self.layer.connections[conn] = object()
        self.context.client.alpn = b"h2"
        flows = []
        for _ in range(2):
            flow, stream, event = self.request(conn)
            flows.append(flow)
            self.assertEqual(list(self.layer.get_connection(event)), [])
            self.assertIs(stream.received[0].reply[0], conn)
            self.assertEqual(self.g.snapshot(flow)["protocol"], "HTTP/2")
        self.assertTrue(self.g.snapshot(flows[1])["reused"])
        self.assertIsNone(self.g.snapshot(flows[1])["idle_s"])

    def test_via_spec_does_not_match_another_route(self):
        direct = connection.Server(address=("private", 80))
        direct.state = connection.ConnectionState.OPEN
        self.layer.connections[direct] = object()
        via = connection.Server(address=direct.address)
        via.via = ("http", ("127.0.0.1", 43210))
        flow, _, event = self.request(via)
        commands_out = list(self.layer.get_connection(event))
        self.assertTrue(commands_out)
        self.assertNotEqual(self.g.snapshot(flow)["server_conn_id"],
                            self.g._generation + ":" + str(direct.id))
        self.assertTrue(self.g.snapshot(flow)["via_proxy"])

    def test_unknown_version_no_global_patch(self):
        self.g.done()
        with patch.object(adapter, "version", return_value="999.0"):
            self.assertFalse(self.g.running())
            self.assertIs(HttpLayer.get_connection, self.original_get)
            conn = connection.Server(address=("private", 80))
            self.g.server_connect(SimpleNamespace(server=conn))
            self.assertEqual(self.g.stats()["connections"], 1)


class GeneratorDelegationTests(unittest.TestCase):
    def test_send_throw_close_and_return_preserved(self):
        observed, final = [], []

        def source():
            try:
                value = yield "first"
                try:
                    yield value
                except ValueError:
                    yield "caught"
                return 42
            finally:
                final.append(True)

        def wrapped():
            return (yield from adapter._ObservedCommands(source(), lambda: observed.append(True)))

        generator = wrapped()
        self.assertEqual(next(generator), "first")
        self.assertEqual(generator.send("sent"), "sent")
        self.assertEqual(generator.throw(ValueError("test")), "caught")
        with self.assertRaises(StopIteration) as stopped:
            next(generator)
        self.assertEqual(stopped.exception.value, 42)
        self.assertEqual(len(observed), 3)
        self.assertEqual(final, [True])
        second = wrapped()
        next(second)
        second.close()
        self.assertEqual(final, [True, True])


class FakeWriter:
    def __init__(self):
        self.closed = False

    def get_extra_info(self, name):
        return ("127.0.0.1", 43210)

    def close(self):
        self.closed = True


class ProbeHandler(ConnectionHandler):
    """Exercise unmodified open_connection without starting a proxy or socket."""
    def __init__(self):
        self.client = connection.Client(peername=("127.0.0.1", 1), sockname=("127.0.0.1", 2))
        self.transports = {}
        self.max_conns = defaultdict(lambda: asyncio.Semaphore(5))
        self.seen = []
        self.hooks = []
        self.connected_entered = asyncio.Event()
        self.hold_connected_hook = False

    def log(self, *args, **kwargs):
        pass

    async def handle_hook(self, hook):
        self.hooks.append(hook)
        if isinstance(hook, server_hooks.ServerConnectedHook):
            self.connected_entered.set()
            if self.hold_connected_hook:
                await asyncio.Event().wait()

    async def server_event(self, event):
        self.seen.append(event)


@unittest.skipUnless(adapter.transport_capabilities()["version"] == "12.2.3", "12.2.3 cancellation contract")
class CancellationFeasibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_semaphore_wait_does_not_unblock_open_waiter(self):
        handler = ProbeHandler()
        conn = connection.Server(address=("127.0.0.1", 43210))
        handler.max_conns[conn.address] = asyncio.Semaphore(0)
        command = commands.OpenConnection(conn)
        task = asyncio.create_task(handler.open_connection(command))
        handler.transports[conn] = ConnectionIO(handler=task)
        await asyncio.sleep(0)
        handler.close_connection(conn)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(any(isinstance(h, server_hooks.ServerConnectHook) for h in handler.hooks))
        self.assertFalse(any(isinstance(e, events.OpenConnectionCompleted) for e in handler.seen))
        # The OpenConnection command remains unresolved; a generic cancel timer
        # would hang the HTTP waiter and retain this transport entry.
        self.assertIn(conn, handler.transports)

    async def test_cancel_connected_hook_leaves_installed_writer_unclosed(self):
        handler = ProbeHandler()
        handler.hold_connected_hook = True
        conn = connection.Server(address=("127.0.0.1", 43210))
        command = commands.OpenConnection(conn)
        writer = FakeWriter()
        with patch("mitmproxy.proxy.server.asyncio.open_connection", return_value=(object(), writer)):
            task = asyncio.create_task(handler.open_connection(command))
            handler.transports[conn] = ConnectionIO(handler=task)
            await asyncio.wait_for(handler.connected_entered.wait(), 1)
            handler.close_connection(conn)
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(writer.closed)
        self.assertIn(conn, handler.transports)
        self.assertFalse(any(isinstance(e, events.OpenConnectionCompleted) for e in handler.seen))
        self.assertFalse(any(isinstance(h, server_hooks.ServerDisconnectedHook) for h in handler.hooks))
        writer.close()  # Explicit cleanup of our fake, not production code.


if __name__ == "__main__":
    unittest.main()
