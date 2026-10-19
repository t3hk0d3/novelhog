"""Image builders, a local HTTP server and a self-signed certificate for network tests."""

import datetime
import io
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from PIL import Image


def make_image(
    fmt: str, size: tuple[int, int] = (10, 10), mode: str = "RGB", **save: object
) -> bytes:
    """Encode a solid-colour image."""
    color = (200, 10, 10, 128) if mode == "RGBA" else (200, 10, 10)
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, fmt, **save)
    return out.getvalue()


def make_animated(fmt: str) -> bytes:
    """Encode a two-frame animation (first frame red, second blue)."""
    frames = [Image.new("RGB", (8, 8), "red"), Image.new("RGB", (8, 8), "blue")]
    out = io.BytesIO()
    frames[0].save(out, fmt, save_all=True, append_images=frames[1:], duration=50, loop=0)
    return out.getvalue()


class Server:
    """Local HTTP server whose routes map a path to (status, headers, body) or a callable."""

    def __init__(self, tls: tuple[Path, Path] | None = None) -> None:
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.hits: list[tuple[str, str | None]] = []
        self.drip: dict[str, float] = {}  # path -> seconds between body bytes
        self.sni: list[str | None] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # keep-alive, like real servers

            def do_GET(self) -> None:
                outer.hits.append((self.path, self.headers.get("Host")))
                status, headers, body = outer.routes.get(self.path, (404, {}, b""))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    delay = outer.drip.get(self.path)
                    if delay is None:
                        self.wfile.write(body)
                    else:
                        for i in range(len(body)):
                            self.wfile.write(body[i : i + 1])
                            self.wfile.flush()
                            time.sleep(delay)
                except OSError:
                    pass

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.scheme = "http"
        if tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            context.sni_callback = lambda _sock, name, _ctx: self.sni.append(name)
            self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
            self.scheme = "https"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"{self.scheme}://{host}:{self.port}{path}"


def make_cert(directory: Path, hostname: str) -> tuple[Path, Path]:
    """Write a self-signed certificate valid for ``hostname``; returns (cert, key) paths."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path
