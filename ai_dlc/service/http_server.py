"""Small standard-library HTTP host for webhooks and health probes."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ssl
import threading
from urllib.parse import urlsplit

from ..github.webhook import MAX_WEBHOOK_BYTES


class ServiceHttpServer:
    def __init__(self, web, runtime, *, webhook_endpoint=None):
        self.web, self.runtime, self.webhook_endpoint = web, runtime, webhook_endpoint
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ai-dlc-agent"
            sys_version = ""

            def log_message(self, *_args):
                return

            def _json(self, status, document):
                body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/health/live":
                    health = outer.runtime.health()
                    self._json(200 if health["live"] else 503, {"live": health["live"]})
                elif path == "/health/ready":
                    health = outer.runtime.health()
                    self._json(200 if health["ready"] else 503, health)
                else:
                    self._json(404, {"error": "NOT_FOUND"})

            def do_POST(self):
                path = urlsplit(self.path).path
                if path != "/hooks/github":
                    self._json(404, {"error": "NOT_FOUND"})
                    return
                if outer.webhook_endpoint is None or not outer.runtime.scheduler.accepting:
                    self._json(503, {"error": "INTAKE_UNAVAILABLE"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self._json(400, {"error": "WEBHOOK_HEADERS"})
                    return
                if length < 0 or length > MAX_WEBHOOK_BYTES:
                    self._json(413, {"error": "WEBHOOK_SIZE"})
                    return
                response = outer.webhook_endpoint.handle("POST", path, self.headers,
                                                         self.rfile.read(length))
                self.send_response(response.status)
                for name, value in response.headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(response.body)

        self._server = ThreadingHTTPServer((web.bind_host, web.port), Handler)
        self._server.daemon_threads = True
        try:
            if web.tls_mode == "direct":
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(str(web.tls_certificate_file), str(web.tls_private_key_file))
                self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        except Exception:
            self._server.server_close()
            raise
        self._thread = None

    @property
    def address(self):
        return self._server.server_address

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="ai-dlc-http", daemon=True)
        self._thread.start()

    def close_intake(self):
        self.webhook_endpoint = None

    def stop(self):
        self.close_intake()
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
