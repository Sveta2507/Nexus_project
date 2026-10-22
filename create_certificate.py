"""створює локальний tls-сертифікат лише засобами python."""

import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def local_addresses() -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    # до сертифіката додаються loopback і поточні адреси локальних мережевих інтерфейсів
    values = {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None):
            address = result[4][0].split("%")[0]
            values.add(ipaddress.ip_address(address))
    except (OSError, ValueError):
        pass
    return sorted(values, key=str)


def main() -> None:
    output = Path(__file__).resolve().parent / "certs"
    output.mkdir(exist_ok=True)
    # закритий ключ зберігається без пароля, бо сертифікат призначений лише для локальної демонстрації
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Nexus Local Development")])
    alternatives: list[x509.GeneralName] = [x509.DNSName("localhost")]
    alternatives.extend(x509.IPAddress(address) for address in local_addresses())
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(alternatives), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    (output / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (output / "cert.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    print(f"Створено: {output / 'cert.pem'}")
    print("Локальні адреси в сертифікаті:", ", ".join(map(str, local_addresses())))
    print("Для телефона імпортуйте cert.pem як довірений сертифікат або підтвердьте виняток у браузері.")


if __name__ == "__main__":
    main()
