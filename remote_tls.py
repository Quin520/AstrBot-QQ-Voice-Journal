"""Private server certificate and a separate upload-only credential."""

import datetime
import hashlib
import secrets
import ssl
from pathlib import Path


class RemoteIdentity:
    def __init__(self, directory: Path):
        self.directory = directory / "remote"
        self.directory.mkdir(exist_ok=True)
        self.token_path = self.directory / "upload-token.txt"
        if not self.token_path.exists():
            self.token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        self.token = self.token_path.read_text(encoding="utf-8").strip()
        if len(self.token) < 32:
            raise ValueError("Invalid upload token")
        self.cert_path, self.key_path = self.directory / "server.pem", self.directory / "server.key"
        if not self.cert_path.exists() and not self.key_path.exists():
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.x509.oid import NameOID

            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Voice Journal Receiver")])
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .sign(key, hashes.SHA256())
            )
            self.key_path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        der = ssl.PEM_cert_to_DER_cert(self.cert_path.read_text(encoding="utf-8"))
        self.fingerprint = hashlib.sha256(der).hexdigest()
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(self.cert_path, self.key_path)

    def rotate(self) -> None:
        token = secrets.token_urlsafe(32)
        self.token_path.write_text(token, encoding="utf-8")
        self.token = token
