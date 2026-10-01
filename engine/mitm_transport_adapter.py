"""Read-only mitmproxy transport observation, isolated behind a version contract.

P1 deliberately stops at the feasibility gate. ``reuse=False`` does not exclude
an already connected context server; pending establishment is shared, and tunneled
TLS connections do not necessarily own a ConnectionHandler transport. Neither
header injection, flow.kill(), nor changing connection.error is a safe substitute
for targeted transport control. No transport, header, body, or retry is modified.
"""
from __future__ import annotations

import inspect
from importlib.metadata import PackageNotFoundError, version
import weakref

SUPPORTED_VERSION = "12.2.3"
_CONTROL_REASON = (
    "Transport control unavailable: context/pending connection isolation and "
    "targeted tunnel cancellation have not passed the compatibility gate."
)
_observers: weakref.WeakSet = weakref.WeakSet()
_originals = None
_wrappers = None


def transport_capabilities() -> dict:
    try:
        installed = version("mitmproxy")
    except PackageNotFoundError:
        installed = "unavailable"
    observation = False
    if installed == SUPPORTED_VERSION:
        try:
            from mitmproxy.proxy.layers.http import HttpLayer
            observation = (
                tuple(inspect.signature(HttpLayer.get_connection).parameters)
                == ("self", "event", "reuse")
                and tuple(inspect.signature(HttpLayer.event_to_child).parameters)
                == ("self", "child", "event")
            )
        except (ImportError, AttributeError, ValueError):
            pass
    return {
        "supported": False,
        "version": installed,
        "reason": _CONTROL_REASON,
        "deadlines": False,
        "http1_reuse_policy": False,
        "observation": observation,
        "observation_reason": None if observation else "Requires mitmproxy 12.2.3 observation interfaces.",
    }


def _notify(method, *args):
    for observer in tuple(_observers):
        try:
            getattr(observer, method)(*args)
        except Exception:
            # Diagnostics must never alter the transport command stream.
            observer.observation_errors += 1


class _ObservedCommands:
    """PEP 380 delegation with an observation before each outward command."""
    def __init__(self, generator, observe):
        self.generator, self.observe = generator, observe

    def __iter__(self):
        return self

    def _out(self, value):
        self.observe()
        return value

    def __next__(self):
        return self._out(next(self.generator))

    def send(self, value):
        return self._out(self.generator.send(value))

    def throw(self, *args):
        return self._out(self.generator.throw(*args))

    def close(self):
        return self.generator.close()


def install(observer) -> bool:
    """Install once process-wide; observers are weakly held. Event-loop thread only."""
    global _originals, _wrappers
    if not transport_capabilities()["observation"]:
        return False
    from mitmproxy.proxy.layers.http import HttpLayer, GetHttpConnectionCompleted
    _observers.add(observer)
    if _originals is not None:
        return True
    original_get = HttpLayer.get_connection
    original_child = HttpLayer.event_to_child

    def pending(self, event):
        stream = self.command_sources.get(event)
        flow = getattr(stream, "flow", None)
        if flow is not None:
            for conn, waiters in self.waiting_for_establishment.items():
                if any(waiter is event for waiter in waiters):
                    _notify("connection_pending", flow, conn)
                    break

    def get_connection(self, event, *, reuse=True):
        result = yield from _ObservedCommands(
            original_get(self, event, reuse=reuse), lambda: pending(self, event)
        )
        # Joining an existing pending connection emits no command.
        pending(self, event)
        return result

    def event_to_child(self, child, event):
        if isinstance(event, GetHttpConnectionCompleted):
            flow = getattr(child, "flow", None)
            if flow is not None:
                conn, error = event.reply
                if conn is not None and not error:
                    _notify("connection_selected", flow, conn)
                else:
                    _notify("connection_selection_failed", flow)
        return (yield from original_child(self, child, event))

    _originals = (original_get, original_child)
    _wrappers = (get_connection, event_to_child)
    HttpLayer.get_connection = get_connection
    HttpLayer.event_to_child = event_to_child
    return True


def uninstall(observer) -> None:
    """Idempotent; do not overwrite instrumentation installed by another addon."""
    global _originals, _wrappers
    _observers.discard(observer)
    if _observers or _originals is None:
        return
    from mitmproxy.proxy.layers.http import HttpLayer
    if HttpLayer.get_connection is _wrappers[0]:
        HttpLayer.get_connection = _originals[0]
    if HttpLayer.event_to_child is _wrappers[1]:
        HttpLayer.event_to_child = _originals[1]
    _originals = _wrappers = None
