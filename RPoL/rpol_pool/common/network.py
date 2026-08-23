"""
Minimal length-prefixed message transport over TCP sockets.

Every message is an arbitrary Python object (dict) serialized with pickle,
prefixed by an 8-byte big-endian length header. This is used for both the
manager<->worker control channel and for shipping model state_dicts, which
can be tens/hundreds of MB, so we stream in chunks rather than relying on a
single send()/recv() call.
"""
import pickle
import socket
import struct

# 64MB chunks when streaming large payloads (model weights)
_CHUNK = 1 << 26


def send_msg(sock: socket.socket, obj) -> None:
    payload = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    header = struct.pack(">Q", len(payload))
    sock.sendall(header)
    # stream in chunks to avoid giant single sendall on huge tensors
    mv = memoryview(payload)
    for i in range(0, len(mv), _CHUNK):
        sock.sendall(mv[i:i + _CHUNK])


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(_CHUNK, n - len(buf)))
        if not chunk:
            raise ConnectionError("socket closed while reading message")
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock: socket.socket):
    header = _recv_exact(sock, 8)
    (length,) = struct.unpack(">Q", header)
    payload = _recv_exact(sock, length)
    return pickle.loads(payload)


def make_server_socket(host: str, port: int, backlog: int = 16) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(backlog)
    return s


def connect_with_retry(host: str, port: int, retries: int = 300, delay: float = 2.0) -> socket.socket:
    import time
    last_err = None
    for _ in range(retries):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((host, port))
            return s
        except OSError as e:
            last_err = e
            time.sleep(delay)
    raise ConnectionError(f"could not connect to {host}:{port}: {last_err}")
