from copy import deepcopy
from contextlib import redirect_stdout
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
from io import StringIO
import json
from pathlib import Path
import ssl
import threading
from types import MappingProxyType
import unittest

from ai_dlc.config.loader import (assert_service_isolation, inspect_service_readiness,
                                  load_connection, load_service, parse_service)
from ai_dlc.config.types import NetworkConfig, TlsConfig, TransportConfig, TransportRoute
from ai_dlc.cli import main
from ai_dlc.errors import AgentError
from ai_dlc.evaluation.offline import OfflineNetworkGuard
from ai_dlc.transport import BackendFailure, HttpRequest, HttpResponse, HttpTransport
from ai_dlc.validation import read_json


ROOT = Path(__file__).resolve().parents[1]


class FakeBackend:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def exchange(self, destination, request, **options):
        self.calls.append((destination, request, options))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def transport_config(*, max_response_bytes=1024, attempts=3, tls=None, port=443):
    route = TransportRoute("localhost", port, (ipaddress.ip_network("127.0.0.0/8"),))
    return TransportConfig(
        tls or TlsConfig("system"), "none", "system",
        MappingProxyType({("localhost", port): route}), 2, 2, max_response_bytes, attempts,
    )


class ServiceConfigTests(unittest.TestCase):
    def setUp(self):
        self.raw = read_json(ROOT / "config" / "service.example.json")
        self.connection = load_connection(ROOT / "config" / "production.example.json")

    def assert_code(self, code, callback):
        with self.assertRaises(AgentError) as error:
            callback()
        self.assertEqual(error.exception.code, code)
        return error.exception

    def test_shipped_service_loads_offline_but_is_not_ready(self):
        with OfflineNetworkGuard() as network:
            bundle = load_service(ROOT / "config" / "service.example.json")
            readiness = inspect_service_readiness(bundle, environment={})
        self.assertEqual(network.attempts, 0)
        self.assertEqual(readiness.status, "configuration_pending")
        self.assertIn("TRANSPORT_ROUTE_ADDRESSES_UNCONFIGURED", readiness.reasons)
        self.assertIn("CREDENTIAL_VALUE_UNAVAILABLE", readiness.reasons)
        self.assertIn("MODEL_NAME_UNAVAILABLE", readiness.reasons)
        self.assertIn("MODEL_LIMITS_UNCONFIGURED", readiness.reasons)
        self.assertNotIn("AI_DLC_", str(readiness.as_dict()))

    def test_service_cli_reports_pending_without_exposing_references(self):
        output = StringIO()
        with OfflineNetworkGuard() as network, redirect_stdout(output):
            code = main(["validate-service", "--service", str(ROOT / "config" / "service.example.json"),
                         "--require-ready"])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(network.attempts, 0)
        self.assertEqual(report["readiness"]["status"], "configuration_pending")
        self.assertNotIn("AI_DLC_", output.getvalue())

    def test_service_schema_types_tls_modes_and_paths_are_strict(self):
        cases = [
            (lambda c: c["transport"].update(read_retry_attempts=0), "CONFIG_TYPE"),
            (lambda c: c["transport"]["proxy"].update(mode="environment"), "CONFIG_VALUE"),
            (lambda c: c["transport"]["tls"].update(ca_bundle_file="ca.pem"), "CONFIG_UNKNOWN_FIELD"),
            (lambda c: c["web"].update(port=70000), "CONFIG_TYPE"),
            (lambda c: c["web"].update(bind_host="0.0.0.0"), "CONFIG_VALUE"),
            (lambda c: c["limits"].update(model_timeout_seconds=True), "CONFIG_TYPE"),
            (lambda c: c["github"].update(private_key_file="../var/production/state/key.pem"),
             "CONFIG_PATH_OVERLAP"),
            (lambda c: c["transport"]["routes"].append(
                {"host": "github.internal.example", "port": 443, "address_ranges": []}),
             "CONFIG_DUPLICATE"),
        ]
        for mutate, code in cases:
            with self.subTest(code=code):
                raw = deepcopy(self.raw)
                mutate(raw)
                self.assert_code(code, lambda: parse_service(raw, base_dir=ROOT / "config",
                                                              connection=self.connection))

    def test_github_api_version_is_optional_but_strict(self):
        raw = deepcopy(self.raw)
        raw["github"]["api_version"] = "2022-11-28"
        self.assertEqual(parse_service(raw, base_dir=ROOT / "config",
                                       connection=self.connection).github.api_version,
                         "2022-11-28")
        for value in (20221128, "2022-1-28", "2022-99-99", "v3"):
            with self.subTest(value=value):
                changed = deepcopy(self.raw)
                changed["github"]["api_version"] = value
                self.assert_code("CONFIG_TYPE" if type(value) is not str else "CONFIG_VALUE",
                                 lambda changed=changed: parse_service(
                                     changed, base_dir=ROOT / "config", connection=self.connection))

    def test_production_external_service_destination_is_rejected_offline(self):
        self.raw["github"]["api_base_url"] = "https://api.openai.com/v1"
        with OfflineNetworkGuard() as network:
            self.assert_code("NETWORK_DENIED", lambda: parse_service(
                self.raw, base_dir=ROOT / "config", connection=self.connection))
        self.assertEqual(network.attempts, 0)

    def test_service_isolation_rejects_shared_credentials_after_paths_are_separate(self):
        bundle = load_service(ROOT / "config" / "service.example.json")
        directories = MappingProxyType({name: ROOT / "var" / "isolated" / name
                                        for name in bundle.connection.directories})
        other_connection = replace(bundle.connection, directories=directories)
        other_execution = replace(bundle.service.execution,
                                  artifact_root=ROOT / "var" / "isolated" / "artifacts")
        other_service = replace(bundle.service, execution=other_execution)
        other_bundle = replace(bundle, connection=other_connection, service=other_service)
        self.assert_code("CONFIG_CREDENTIAL_OVERLAP",
                         lambda: assert_service_isolation(bundle, other_bundle))

        nested_service = replace(other_service, credential_envs=frozenset({"ISOLATED_SECRET"}),
                                 credential_files=frozenset({bundle.connection.directories["workspace"] / "key.pem"}))
        nested_bundle = replace(other_bundle, service=nested_service)
        self.assert_code("CONFIG_CREDENTIAL_OVERLAP",
                         lambda: assert_service_isolation(bundle, nested_bundle))


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.network = NetworkConfig("production", False, frozenset({"localhost"}), frozenset())

    def assert_code(self, code, callback):
        with self.assertRaises(AgentError) as error:
            callback()
        self.assertEqual(error.exception.code, code)
        return error.exception

    @staticmethod
    def response(status=200, body=b"ok", headers=None):
        return HttpResponse(status, MappingProxyType(headers or {}), body)

    def test_policy_and_dns_are_checked_before_backend(self):
        backend = FakeBackend(self.response())
        resolver_calls = []

        def resolver(host, port):
            resolver_calls.append((host, port))
            return ("203.0.113.7",)

        transport = HttpTransport(self.network, transport_config(), backend=backend, resolver=resolver)
        self.assert_code("NETWORK_DENIED", lambda: transport.request(
            HttpRequest("GET", "https://outside.invalid/data", {})))
        self.assertEqual(resolver_calls, [])
        self.assert_code("NETWORK_DENIED", lambda: transport.request(
            HttpRequest("GET", "https://localhost/data", {})))
        self.assertEqual(len(resolver_calls), 1)
        self.assertEqual(backend.calls, [])

    def test_redirects_are_never_followed(self):
        backend = FakeBackend(self.response(302, headers={"location": "https://outside.invalid"}))
        transport = HttpTransport(self.network, transport_config(), backend=backend,
                                  resolver=lambda _host, _port: ("127.0.0.1",))
        self.assert_code("NETWORK_REDIRECT", lambda: transport.request(
            HttpRequest("GET", "https://localhost/data", {})))
        self.assertEqual(len(backend.calls), 1)

    def test_only_reads_retry_and_write_response_loss_is_unknown(self):
        delays = []
        backend = FakeBackend(BackendFailure("timeout", request_sent=False),
                              BackendFailure("response", request_sent=True), self.response())
        transport = HttpTransport(self.network, transport_config(), backend=backend,
                                  resolver=lambda _host, _port: ("127.0.0.1",),
                                  sleeper=delays.append)
        response = transport.request(HttpRequest("GET", "https://localhost/data", {}))
        self.assertEqual(response.status, 200)
        self.assertEqual(delays, [1, 2])
        self.assertEqual(len(backend.calls), 3)

        write_backend = FakeBackend(BackendFailure("response", request_sent=True), self.response())
        write_transport = HttpTransport(self.network, transport_config(), backend=write_backend,
                                        resolver=lambda _host, _port: ("127.0.0.1",))
        error = self.assert_code("EFFECT_UNKNOWN", lambda: write_transport.request(
            HttpRequest("POST", "https://localhost/items", {"Authorization": "Bearer SECRET"}, b"{}")))
        self.assertEqual(len(write_backend.calls), 1)
        self.assertNotIn("SECRET", str(error.as_dict()))

    def test_response_limit_and_missing_ca_fail_closed(self):
        backend = FakeBackend(self.response(body=b"too-large"))
        transport = HttpTransport(self.network, transport_config(max_response_bytes=3),
                                  backend=backend, resolver=lambda _host, _port: ("127.0.0.1",))
        self.assert_code("RESPONSE_LIMIT", lambda: transport.request(
            HttpRequest("GET", "https://localhost/data", {})))
        missing = ROOT / "var" / "does-not-exist" / "ca.pem"
        tls_transport = HttpTransport(self.network, transport_config(tls=TlsConfig("custom_ca", missing)),
                                      backend=backend,
                                      resolver=lambda _host, _port: self.fail("DNS must not run"))
        self.assert_code("TRANSPORT_NOT_CONFIGURED", lambda: tls_transport.request(
            HttpRequest("GET", "https://localhost/data", {})))

    def test_local_tls_fixture_uses_configured_ca_and_verified_hostname(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"fixture":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *args):
                pass

        certificate = ROOT / "tests" / "fixtures" / "localhost-cert.pem"
        private_key = ROOT / "tests" / "fixtures" / "localhost-key.pem"
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, private_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            configured = HttpTransport(
                self.network, transport_config(tls=TlsConfig("custom_ca", certificate), port=port),
                resolver=lambda _host, _port: ("127.0.0.1",),
            )
            response = configured.request(HttpRequest("GET", f"https://localhost:{port}/health", {}))
            self.assertEqual(response.status, 200)
            self.assertEqual(response.body, b'{"fixture":"ok"}')

            untrusted = HttpTransport(self.network, transport_config(port=port),
                                      resolver=lambda _host, _port: ("127.0.0.1",))
            self.assert_code("TLS_ERROR", lambda: untrusted.request(
                HttpRequest("GET", f"https://localhost:{port}/health", {})))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_header_and_url_values_are_constrained_without_echo(self):
        backend = FakeBackend(self.response())
        transport = HttpTransport(self.network, transport_config(), backend=backend,
                                  resolver=lambda _host, _port: ("127.0.0.1",))
        for request in (
            HttpRequest("GET", "https://localhost/data#fragment", {}),
            HttpRequest("GET", "https://localhost/data", {"Host": "outside.invalid"}),
            HttpRequest("POST", "https://localhost/data", {"X-Test": "SECRET\r\nInjected"}),
        ):
            with self.subTest(url=request.url[:20]):
                error = self.assert_code("NETWORK_URL" if "#" in request.url else "HTTP_REQUEST",
                                         lambda request=request: transport.request(request))
                self.assertNotIn("SECRET", str(error.as_dict()))
        self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
