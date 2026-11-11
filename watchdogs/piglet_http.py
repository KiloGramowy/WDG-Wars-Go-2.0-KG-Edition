"""Bounded authenticated local Collection HTTP transport; no UI, XP or network setup."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from http.client import LineTooLong
import hmac
import ipaddress
import json
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .piglet_collection import CollectionError, MAX_CHUNK

MAX_JSON = 16384
MAX_REPLY = 8192
PEER_ACTIVITY_SECONDS = 30.0
MAX_PEER_SESSIONS = 16
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class _BoundedHeaders:
    def __init__(self, stream):
        self.stream, self.remaining = stream, MAX_JSON

    def readline(self, size=-1):
        line = self.stream.readline(min(size, self.remaining + 1) if size >= 0 else self.remaining + 1)
        self.remaining -= len(line)
        if self.remaining < 0:
            raise LineTooLong("Collection headers")
        return line


def signed_receipt(receipt, token):
    """Sign only the explicit protocol-1 fields, including its final LF byte."""
    fields = ("protocol", "collector_id", "sha256", "size", "import_id", "records", "wifi", "ble", "duplicates")
    value = {key: receipt[key] for key in fields}
    if (value["protocol"] != 1 or not _HEX32.fullmatch(value["collector_id"])
            or not _HEX32.fullmatch(value["import_id"]) or not _HEX64.fullmatch(value["sha256"])
            or any(type(value[key]) is not int or value[key] < 0
                   for key in ("size", "records", "wifi", "ble", "duplicates"))
            or value["wifi"] + value["ble"] + value["duplicates"] != value["records"]):
        raise ValueError("Invalid durable receipt")
    canonical = "PC1\n" + "\n".join(str(value[key]) for key in fields[1:]) + "\n"
    value["signature"] = hmac.digest(token.encode("utf-8"), canonical.encode("ascii"), "sha256").hex()
    return value


class _LocalServer(HTTPServer):
    allow_reuse_address = True
    request_queue_size = 4

    def __init__(self, address, handler, owner):
        self.owner = owner
        self._slots = threading.BoundedSemaphore(owner.max_clients)
        self._workers = set()
        self._connections = set()
        self._expired = {}
        self._condition = threading.Condition()
        self.stopping = threading.Event()
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if self.stopping.is_set() or not self._slots.acquire(blocking=False):
            # Do not create an unbounded thread just to refuse excess clients.
            payload = b'{"error":"Collector busy; retry later"}'
            try:
                # Receive a bounded header prefix before replying. Sending a
                # refusal before a normal client finishes sendall() can abort
                # that client's TCP connection instead of delivering the 503.
                request.settimeout(.1)
                prefix, until = bytearray(), time.monotonic() + .1
                try:
                    while b"\r\n\r\n" not in prefix and len(prefix) < MAX_JSON:
                        remaining = until - time.monotonic()
                        if remaining <= 0:
                            break
                        request.settimeout(remaining)
                        data = request.recv(min(4096, MAX_JSON - len(prefix)))
                        if not data:
                            break
                        prefix.extend(data)
                except socket.timeout:
                    pass
                request.settimeout(.1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                b"Content-Length: " + str(len(payload)).encode("ascii")
                                + b"\r\nConnection: close\r\n\r\n" + payload)
            except OSError:
                pass
            self._finish_connection(request)
            return
        worker = threading.Thread(target=self._serve_request, args=(request, client_address),
                                  name="piglet-http-client", daemon=True)
        with self._condition:
            self._workers.add(worker)
            self._connections.add(request)
        try:
            worker.start()
        except Exception:
            with self._condition:
                self._workers.discard(worker)
                self._connections.discard(request)
            self._slots.release()
            self.shutdown_request(request)
            raise

    @staticmethod
    def _disconnect(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @staticmethod
    def _expire(connection, expired):
        expired.set()
        try:
            # Interrupt reads without cutting a small, framed timeout reply
            # in half. STOP uses SHUT_RDWR separately when no reply is owed.
            connection.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    def expired(self, connection):
        event = self._expired.get(connection)
        return event is not None and event.is_set()

    def _finish_connection(self, connection):
        # A close with unread request bytes can reset TCP and discard our
        # already flushed error reply (notably on Windows). Half-close the
        # reply first, then drain only a bounded amount for a bounded time.
        try:
            connection.shutdown(socket.SHUT_WR)
            connection.settimeout(.01)
            end, remaining = time.monotonic() + .05, 80 * 1024
            while remaining and time.monotonic() < end:
                data = connection.recv(min(4096, remaining))
                if not data:
                    break
                remaining -= len(data)
        except OSError:
            pass
        self.close_request(connection)

    def _serve_request(self, request, client_address):
        request.settimeout(self.owner.request_timeout)
        expired = threading.Event()
        with self._condition:
            self._expired[request] = expired
        deadline = threading.Timer(self.owner.request_timeout, self._expire, args=(request, expired))
        deadline.daemon = True
        deadline.start()
        try:
            self.finish_request(request, client_address)
        except (OSError, TimeoutError):
            pass
        finally:
            deadline.cancel()
            self._finish_connection(request)
            with self._condition:
                self._connections.discard(request)
                self._expired.pop(request, None)
                self._workers.discard(threading.current_thread())
                self._condition.notify_all()
            self._slots.release()

    def disconnect_clients(self):
        with self._condition:
            for connection in tuple(self._connections):
                self._disconnect(connection)

    def wait_clients(self, timeout):
        end = time.monotonic() + max(0, timeout)
        with self._condition:
            while self._workers:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PigletCollection/1"
    sys_version = ""

    def parse_request(self):
        if self.server.expired(self.connection):
            self.request_version = "HTTP/1.1"
            self._reply(408, {"error": "Collection request timed out; source files are kept"})
            return False
        stream = self.rfile
        self.rfile = _BoundedHeaders(stream)
        try:
            return super().parse_request()
        finally:
            self.rfile = stream

    def log_message(self, _format, *_args):
        # URLs, pairing headers and raw device names must not enter console logs.
        pass

    def handle_expect_100(self):
        self._reply(417, {"error": "Expect/continue is not supported"})
        return False

    def send_error(self, code, message=None, explain=None):
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        self._reply(code, {"error": "Invalid HTTP request"})

    def _reply(self, status, payload):
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
        if len(encoded) > MAX_REPLY:
            status, encoded = 500, b'{"error":"Collector reply exceeded its limit"}'
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)
        self.wfile.flush()
        return status

    def _header(self, name, *, required=False):
        values = self.headers.get_all(name, [])
        if len(values) > 1 or required and len(values) != 1:
            raise CollectionError("Invalid request header")
        return values[0] if values else None

    def _body(self, maximum, *, allow_empty=False):
        if self.headers.get("Transfer-Encoding") is not None:
            raise CollectionError("Chunked requests are not supported")
        if self.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise CollectionError("Encoded requests are not supported", 415)
        value = self._header("Content-Length", required=not allow_empty)
        if value is None:
            return b""
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,9})", value):
            raise CollectionError("Invalid Content-Length")
        length = int(value)
        if length > maximum:
            raise CollectionError("Request body is too large", 413)
        data = self.rfile.read(length)
        if len(data) != length:
            if self.server.expired(self.connection):
                raise TimeoutError()
            raise CollectionError("Incomplete request body")
        return data

    def _json(self):
        try:
            value = json.loads(self._body(MAX_JSON).decode("utf-8"))
        except (ValueError, UnicodeError):
            raise CollectionError("Invalid JSON request") from None
        if not isinstance(value, dict):
            raise CollectionError("JSON object required")
        return value

    @staticmethod
    def _keys(body, names, optional=()):
        if not set(names) <= set(body) or not set(body) <= set(names) | set(optional):
            raise CollectionError("Unexpected JSON fields")

    def _session(self):
        value = self._header("X-Piglet-Session", required=True)
        if not _HEX32.fullmatch(value):
            raise CollectionError("Invalid Collection session", 404)
        return value

    def _dispatch(self):
        owner = self.server.owner
        try:
            if self.server.expired(self.connection):
                raise TimeoutError()
            authorization = self._header("Authorization")
            expected = ("Bearer " + owner.token).encode("utf-8")
            if (authorization is None
                    or not hmac.compare_digest(authorization.encode("utf-8"), expected)):
                self._reply(401, {"error": "Collection authentication required"})
                return
            if self.server.stopping.is_set():
                raise CollectionError("Collector is stopping", 503)
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment or len(self.path) > 512:
                raise CollectionError("Invalid local request target")
            path = parsed.path
            queries = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            if any(len(values) != 1 for values in queries.values()):
                raise CollectionError("Duplicate query argument")
            query = {name: values[0] for name, values in queries.items()}
            store = owner.store
            callback = None
            activity_session = activity_device = None
            if (self.command, path) == ("GET", "/pc/v1/hello"):
                self._keys(query, ())
                if self._body(0, allow_empty=True):
                    raise CollectionError("GET body is not supported")
                result = dict(protocol=1, collector_id=store.collector_id, max_chunk=MAX_CHUNK,
                              source_items=1)
            elif (self.command, path) == ("POST", "/pc/v1/begin"):
                self._keys(query, ())
                body = self._json()
                self._keys(body, ("device_id", "pending_files"), ("source_items",))
                if "source_items" in body:
                    if type(body["source_items"]) is not int or body["source_items"] != 1:
                        raise CollectionError("Unsupported Collection source-item metadata")
                    result = store.begin(body["device_id"], body["pending_files"], source_items=1)
                else:
                    result = store.begin(body["device_id"], body["pending_files"])
                activity_session, activity_device = result["session_id"], body["device_id"]
            elif (self.command, path) == ("POST", "/pc/v1/file"):
                self._keys(query, ())
                body = self._json()
                self._keys(body, ("name", "size", "sha256"), ("source_id",))
                activity_session = self._session()
                if "source_id" in body:
                    if not isinstance(body["source_id"], str) or not re.fullmatch(r"[0-9a-f]{64}", body["source_id"]):
                        raise CollectionError("Invalid Collection source identity")
                    result = store.offer(activity_session, body["name"], body["size"], body["sha256"],
                                         source_id=body["source_id"])
                else:
                    result = store.offer(activity_session, body["name"], body["size"], body["sha256"])
            elif (self.command, path) == ("PUT", "/pc/v1/chunk"):
                self._keys(query, ("id", "offset"))
                if not re.fullmatch(r"(?:0|[1-9][0-9]{0,10})", query["offset"]):
                    raise CollectionError("Invalid chunk offset")
                data = self._body(MAX_CHUNK)
                activity_session = self._session()
                result = store.chunk(activity_session, query["id"], int(query["offset"]), data,
                                     self._header("X-Chunk-SHA256", required=True))
            elif (self.command, path) == ("POST", "/pc/v1/commit"):
                self._keys(query, ())
                body = self._json()
                self._keys(body, ("sha256",))
                activity_session = self._session()
                result = store.commit(activity_session, body["sha256"])
            elif (self.command, path) == ("GET", "/pc/v1/status"):
                self._keys(query, ("id",))
                self._body(0, allow_empty=True)
                activity_session = self._session()
                result = store.status(activity_session, query["id"])
            elif (self.command, path) == ("POST", "/pc/v1/finish"):
                self._keys(query, ())
                self._keys(self._json(), ())
                activity_session = self._session()
                result = store.finish(activity_session)
                callback = result
            else:
                raise CollectionError("Unknown Collection endpoint", 404)
            if "receipt" in result:
                try:
                    result = dict(result, receipt=signed_receipt(result["receipt"], owner.token))
                except (ValueError, TypeError, KeyError):
                    raise CollectionError("Stored receipt is invalid", 503) from None
            status = 202 if path == "/pc/v1/commit" and result.get("state") != "committed" else 200
            reply_status = self._reply(status, result)
            if reply_status in (200, 202) and "error" not in result and activity_session is not None:
                try:
                    activity = getattr(owner, "_record_activity", None)
                    if callable(activity):
                        activity(activity_session, activity_device,
                                 finished=path == "/pc/v1/finish" and result.get("finished") is True)
                except Exception:
                    # Optional telemetry cannot change an already flushed reply.
                    pass
            # This is a scheduling notification only. It must never call stop()
            # synchronously from an HTTP worker or run game presentation/XP code.
            if (callback is not None and reply_status == 200 and owner.on_finish is not None
                    and not self.server.stopping.is_set()):
                try:
                    owner.on_finish(callback)
                except Exception:
                    pass
        except CollectionError as error:
            status = error.status if error.status in (400, 404, 409, 413, 415, 503, 507) else 500
            messages = {400: "Invalid Collection request", 404: "Unknown Collection session, file or endpoint",
                        409: "Transfer state or offset conflict; retry from the durable offset",
                        413: "Request body exceeds the Collection limit", 415: "Encoded request is unsupported",
                        503: "Collector storage is unavailable; source files are kept",
                        507: "Not enough collector storage; source files are kept"}
            payload = {"error": messages.get(status, "Local Collection failed; source files are kept")}
            if error.offset is not None:
                payload["offset"] = error.offset
            self._reply(status, payload)
        except (socket.timeout, TimeoutError):
            self._reply(408, {"error": "Collection request timed out; source files are kept"})
        except (ValueError, TypeError, UnicodeError, KeyError):
            self._reply(400, {"error": "Invalid Collection request"})
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception:
            self._reply(503, {"error": "Local Collection is unavailable; source files are kept"})

    do_GET = do_POST = do_PUT = _dispatch


class PigletHTTPService:
    def __init__(self, store, token, ap_address, port=8765, *, on_finish=None,
                 allow_loopback=False, max_clients=4, request_timeout=5.0):
        address = ipaddress.IPv4Address(ap_address)
        private_ap = any(address in ipaddress.IPv4Network(network)
                         for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
        if str(address) != ap_address or not (private_ap or address.is_loopback and allow_loopback):
            raise ValueError("Collector requires its explicit private AP IPv4 address")
        if (not isinstance(token, str) or not 16 <= len(token.encode("utf-8")) <= 256
                or any(ord(char) < 33 or ord(char) > 126 for char in token)
                or type(port) is not int or not 0 <= port <= 65535
                or type(max_clients) is not int or not 1 <= max_clients <= 8
                or not .1 <= request_timeout <= 30):
            raise ValueError("Invalid Collection HTTP configuration")
        self.store, self.token, self.ap_address, self.port = store, token, ap_address, port
        self.on_finish, self.max_clients, self.request_timeout = on_finish, max_clients, float(request_timeout)
        self._server = None
        self._thread = None
        self._lock = threading.RLock()
        self._peer_lock = threading.Lock()
        self._peers = {}
        self._activity_clock = time.monotonic

    def _prune_peers(self, now):
        for session, (_, touched) in tuple(self._peers.items()):
            if now - touched >= PEER_ACTIVITY_SECONDS:
                self._peers.pop(session, None)

    def _record_activity(self, session_id, device_id=None, *, finished=False):
        """Track verified successful RPCs only; never write transfer metadata.

        A reconnect can poll an already durable session after this service has
        restarted. Resolve that cold binding read-only once; regular polls use
        only the bounded in-memory registry. Telemetry never changes a reply.
        """
        try:
            server = self._server
            if server is None or server.stopping.is_set() or not _HEX32.fullmatch(session_id):
                return
            with self._peer_lock:
                self._prune_peers(self._activity_clock())
                cached = self._peers.get(session_id)
            if cached is not None:
                device_id = cached[0]
            elif device_id is None:
                if not self.store._lock.acquire(blocking=False):
                    return
                try:
                    device_id = self.store._session(session_id, writable=False)["device"]
                finally:
                    self.store._lock.release()
            if not isinstance(device_id,str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,64}",device_id):
                return
            with self._peer_lock:
                if server.stopping.is_set():
                    return
                now = self._activity_clock()
                self._prune_peers(now)
                if finished:
                    for key,(device,_) in tuple(self._peers.items()):
                        if device == device_id:
                            self._peers.pop(key,None)
                    return
                if session_id not in self._peers and len(self._peers) >= MAX_PEER_SESSIONS:
                    oldest = min(self._peers, key=lambda key:self._peers[key][1])
                    self._peers.pop(oldest)
                self._peers[session_id] = (device_id,now)
        except Exception:
            # Activity is optional presentation metadata, not an import receipt.
            return

    def connected_devices(self):
        """Recent authenticated protocol peers, independent of pending files."""
        server, thread = self._server, self._thread
        if server is None or thread is None or not thread.is_alive() or server.stopping.is_set():
            return []
        with self._peer_lock:
            self._prune_peers(self._activity_clock())
            return sorted({device for device,_ in self._peers.values()})

    @property
    def address(self):
        with self._lock:
            return self._server.server_address if self._server is not None else (self.ap_address, self.port)

    @property
    def running(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive() and not self._server.stopping.is_set()

    def start(self):
        with self._lock:
            if self._server is not None:
                if self.running:
                    return self.address
                raise RuntimeError("Previous Collection HTTP service has not stopped")
            server = _LocalServer((self.ap_address, self.port), _Handler, self)
            with self._peer_lock:
                self._peers.clear()
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05},
                                      name="piglet-http", daemon=True)
            self._server, self._thread = server, thread
            try:
                thread.start()
            except Exception:
                server.server_close()
                self._server = self._thread = None
                raise
            return server.server_address

    def stop(self, timeout=3.0):
        with self._lock:
            if self._server is None:
                return True
            server, thread = self._server, self._thread
            if threading.current_thread() in server._workers:
                raise RuntimeError("Schedule Collection stop outside the HTTP callback")
            started = time.monotonic()
            server.stopping.set()
            with self._peer_lock:
                self._peers.clear()
            server.disconnect_clients()
            server.shutdown()
            thread.join(timeout=max(0, timeout - (time.monotonic() - started)))
            server.server_close()
            clean = not thread.is_alive() and server.wait_clients(timeout - (time.monotonic() - started))
            if clean:
                self._server = self._thread = None
            return clean
