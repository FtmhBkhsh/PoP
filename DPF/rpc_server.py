import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


def make_handler(worker):
    """
    Build a JSON-RPC 2.0 request handler bound to this node's Worker, so
    RPC calls read/write the exact same blockchain and task pools the
    main DPF.py loop is using -- not a second, disconnected copy of
    state (that was the root issue behind the earlier REST endpoints
    never having existed at all).

    Every method below takes the worker plus keyword params matching
    the JSON-RPC "params" object, and returns a JSON-serializable
    result. Worker-side thread-safety (self._lock) is handled inside
    the Worker methods being called, not here.
    """

    class RPCHandler(BaseHTTPRequestHandler):

        METHODS = {
            "get_chain":            lambda w, **p: w.get_chain(),
            "receive_block":        lambda w, block: w.receive_block(block),
            "get_pending_tasks":    lambda w, **p: w.get_pending_tasks(),
            "get_processing_tasks": lambda w, **p: w.get_processing_pool(),
            "get_completed_tasks":  lambda w, **p: w.get_completed_pool(),
            "submit_processing":    lambda w, transaction: w.record_processing(transaction),
            "submit_completed":     lambda w, transaction: w.record_completed(transaction),
            "assign_tasks":         lambda w, tasks: w.record_assigned(tasks),
        }

        def log_message(self, fmt, *args):
            logger.debug("[%s] %s", worker.node_id, fmt % args)

        def do_POST(self):
            if self.path != "/rpc":
                self._reply(404, self._error(None, -32601, "Not found"))
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                req = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                self._reply(400, self._error(None, -32700, "Parse error"))
                return

            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params", {})

            handler = self.METHODS.get(method)
            if handler is None:
                self._reply(200, self._error(req_id, -32601, f"Unknown method {method}"))
                return

            try:
                if isinstance(params, dict):
                    result = handler(worker, **params)
                else:
                    result = handler(worker, *params)
            except Exception as e:
                logger.warning("[%s] RPC method %s failed: %s", worker.node_id, method, e)
                self._reply(200, self._error(req_id, -32000, str(e)))
                return

            self._reply(200, {"jsonrpc": "2.0", "result": result, "id": req_id})

        @staticmethod
        def _error(req_id, code, message):
            return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": req_id}

        def _reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return RPCHandler


def start_rpc_server(worker, host="0.0.0.0", port=5000):
    """
    Run the JSON-RPC listener in a background daemon thread so DPF.py's
    main worker loop keeps running unmodified alongside it -- this is
    what was missing this whole time: every peer had a client that
    could call out, but nothing anywhere was listening on :5000.
    """
    handler_cls = make_handler(worker)
    server = ThreadingHTTPServer((host, port), handler_cls)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    logger.info("[%s] JSON-RPC server listening on %s:%d", worker.node_id, host, port)
    return server
