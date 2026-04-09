"""
TCP socket server for MQL5 ↔ Python communication.

Architecture:
    Python runs as the SERVER (listens on localhost:5555)
    MQL5 EA connects as the CLIENT

Protocol: newline-delimited JSON messages over TCP.

Message types:
    MQL5 → Python:
        tick      — raw tick data (bid, ask, time, volume, spread)
        fill      — order fill confirmation
        heartbeat — keepalive
        position  — position state sync on reconnect
        shutdown  — EA is shutting down

    Python → MQL5:
        signal    — trade signal (BUY/SELL with SL/TP/volume)
        modify    — modify SL/TP on active position
        close     — close active position
        flatten   — emergency close all
        heartbeat — keepalive response
        command   — generic command (halt, resume, etc.)

The server is single-threaded using select() for non-blocking I/O.
This is deliberate — the bot is sequential by design.
No asyncio, no threads, no race conditions.
"""

import socket
import select
import json
import time
import logging
from typing import Optional, Callable, Dict, List
from collections import deque


class Message:
    __slots__ = ("type", "data", "timestamp")

    def __init__(self, msg_type: str, data: dict, timestamp: Optional[int] = None):
        self.type = msg_type
        self.data = data
        self.timestamp = timestamp or int(time.time() * 1000)

    def to_json(self) -> str:
        payload = {"type": self.type, "time": self.timestamp}
        payload.update(self.data)
        return json.dumps(payload)

    @classmethod
    def from_json(cls, raw: str) -> "Message":
        d = json.loads(raw)
        msg_type = d.pop("type")
        ts = d.pop("time", None)
        return cls(msg_type, d, ts)


class SocketServer:
    """
    Single-client TCP server for MQL5 EA communication.
    Non-blocking with select() — never blocks the engine loop.
    """

    __slots__ = (
        "_host", "_port", "_buf_size",
        "_server", "_client", "_client_addr",
        "_recv_buffer", "_send_queue",
        "_last_heartbeat_recv", "_last_heartbeat_sent",
        "_hb_interval_ms", "_hb_timeout_ms",
        "_connected", "_log",
        "_handlers",
    )

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5555,
        recv_buffer: int = 65536,
        heartbeat_interval_ms: int = 1000,
        heartbeat_timeout_ms: int = 5000,
    ):
        self._host = host
        self._port = port
        self._buf_size = recv_buffer
        self._hb_interval_ms = heartbeat_interval_ms
        self._hb_timeout_ms = heartbeat_timeout_ms

        self._server: Optional[socket.socket] = None
        self._client: Optional[socket.socket] = None
        self._client_addr = None
        self._recv_buffer: str = ""
        self._send_queue: deque = deque()
        self._last_heartbeat_recv: float = 0
        self._last_heartbeat_sent: float = 0
        self._connected: bool = False
        self._log = logging.getLogger("socket")

        self._handlers: Dict[str, Callable] = {}

    # ── Lifecycle ───────────────────────────────────────────────

    def start(self) -> None:
        """Start listening for MQL5 EA connection."""
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.setblocking(False)
        self._server.bind((self._host, self._port))
        self._server.listen(1)
        self._log.info("Listening on %s:%d", self._host, self._port)

    def stop(self) -> None:
        """Shut down gracefully."""
        if self._client:
            try:
                self.send(Message("command", {"action": "SHUTDOWN"}))
                self._flush_send()
                self._client.close()
            except Exception:
                pass
            self._client = None

        if self._server:
            self._server.close()
            self._server = None

        self._connected = False
        self._log.info("Socket server stopped")

    @property
    def connected(self) -> bool:
        return self._connected

    # ── Handler registration ────────────────────────────────────

    def on(self, msg_type: str, handler: Callable[[Message], None]) -> None:
        """Register handler for a message type."""
        self._handlers[msg_type] = handler

    # ── Main loop call ──────────────────────────────────────────

    def poll(self) -> List[Message]:
        """
        Non-blocking poll. Call this every iteration of the engine loop.

        1. Accept new connections
        2. Read incoming data
        3. Parse complete messages
        4. Send outgoing queue
        5. Handle heartbeats

        Returns list of received messages for the engine to process.
        """
        messages = []

        if self._server is None:
            return messages

        # Accept new connection
        if not self._connected:
            self._try_accept()

        if not self._connected:
            return messages

        # Read incoming
        try:
            readable, writable, errors = select.select(
                [self._client], [self._client] if self._send_queue else [], [self._client], 0
            )
        except (ValueError, OSError):
            self._disconnect("select_error")
            return messages

        if errors:
            self._disconnect("socket_error")
            return messages

        if readable:
            msgs = self._read()
            for msg in msgs:
                if msg.type == "heartbeat":
                    self._last_heartbeat_recv = time.time()
                elif msg.type in self._handlers:
                    self._handlers[msg.type](msg)
                messages.append(msg)

        if writable:
            self._flush_send()

        # Send heartbeat if due
        now = time.time()
        if now - self._last_heartbeat_sent > self._hb_interval_ms / 1000.0:
            self.send(Message("heartbeat", {}))
            self._last_heartbeat_sent = now

        # Check heartbeat timeout
        if self._last_heartbeat_recv > 0:
            elapsed_ms = (now - self._last_heartbeat_recv) * 1000
            if elapsed_ms > self._hb_timeout_ms:
                self._log.warning("Heartbeat timeout (%.0fms)", elapsed_ms)
                self._disconnect("heartbeat_timeout")

        return messages

    # ── Send ────────────────────────────────────────────────────

    def send(self, msg: Message) -> bool:
        """Queue a message for sending."""
        if not self._connected:
            return False
        self._send_queue.append(msg.to_json() + "\n")
        return True

    def send_signal(
        self, action: str, volume: float, sl: float, tp: float, magic: int
    ) -> bool:
        return self.send(Message("signal", {
            "action": action,
            "volume": volume,
            "sl": sl,
            "tp": tp,
            "magic": magic,
        }))

    def send_modify(self, ticket: int, sl: float, tp: float) -> bool:
        return self.send(Message("modify", {
            "ticket": ticket,
            "sl": sl,
            "tp": tp,
        }))

    def send_close(self, ticket: int, reason: str) -> bool:
        return self.send(Message("close", {
            "ticket": ticket,
            "reason": reason,
        }))

    def send_flatten(self, reason: str) -> bool:
        return self.send(Message("flatten", {"reason": reason}))

    # ── Internal ────────────────────────────────────────────────

    def _try_accept(self) -> None:
        try:
            readable, _, _ = select.select([self._server], [], [], 0)
            if readable:
                self._client, self._client_addr = self._server.accept()
                self._client.setblocking(False)
                self._client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._connected = True
                self._recv_buffer = ""
                self._last_heartbeat_recv = time.time()
                self._last_heartbeat_sent = time.time()
                self._log.info("EA connected from %s", self._client_addr)
        except Exception:
            pass

    def _read(self) -> List[Message]:
        messages = []
        try:
            data = self._client.recv(self._buf_size)
            if not data:
                self._disconnect("eof")
                return messages

            self._recv_buffer += data.decode("utf-8")

            # Parse complete messages (newline-delimited)
            while "\n" in self._recv_buffer:
                line, self._recv_buffer = self._recv_buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = Message.from_json(line)
                    messages.append(msg)
                except json.JSONDecodeError:
                    self._log.warning("Invalid JSON: %s", line[:100])

        except BlockingIOError:
            pass
        except ConnectionResetError:
            self._disconnect("connection_reset")
        except Exception as e:
            self._log.exception("Read error: %s", e)
            self._disconnect("read_error")

        return messages

    def _flush_send(self) -> None:
        while self._send_queue:
            data = self._send_queue[0]
            try:
                self._client.sendall(data.encode("utf-8"))
                self._send_queue.popleft()
            except BlockingIOError:
                break
            except (BrokenPipeError, ConnectionResetError):
                self._disconnect("send_error")
                break
            except Exception as e:
                self._log.exception("Send error: %s", e)
                self._disconnect("send_error")
                break

    def _disconnect(self, reason: str) -> None:
        self._log.warning("Disconnected: %s", reason)
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._connected = False
        self._recv_buffer = ""
        self._send_queue.clear()
