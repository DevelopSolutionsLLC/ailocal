"""Issue and verify the TLS material the LAN sidecar serves (decision 019).

Off loopback the proxy is reachable only through the TLS sidecar
(deploy/tls/compose.yaml). ailocal is its own CA: the operator installs
`ca.pem` on each client machine. Name constraints confine that CA to private
IPv4 ranges and `.local`, so a stolen CA key cannot impersonate public sites
to a machine that trusts it. Private keys are 0600 in 0700 directories under
the state root, never mounted whole (the sidecar sees only `server/`), never
printed. Verification is a real TLS handshake, not a parse of the files.
"""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path

from . import policy

DEFAULT_PORT = 4443
#: RFC 1918, RFC 6598 (CGNAT, e.g. Tailscale) and link-local; nothing public.
PERMITTED_NETWORKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                      "100.64.0.0/10", "169.254.0.0/16")
#: Within every client limit (Apple caps TLS server certificates at 825 days).
SERVER_DAYS = 397
CA_DAYS = 3650
#: `ailocal start` reissues inside this window; `ailocal check` fails inside it.
RENEW_SECONDS = 30 * 86400


def root() -> Path:
    return policy.state_root() / "tls"


def ca_cert() -> Path:
    return root() / "ca" / "ca.pem"


def ca_key() -> Path:
    return root() / "ca" / "ca.key"


def server_cert() -> Path:
    return root() / "server" / "server.pem"


def server_key() -> Path:
    return root() / "server" / "server.key"


def port() -> int:
    """Return the sidecar's host port; process env wins, as in Compose."""
    from . import environment
    raw = os.environ.get("AILOCAL_TLS_PORT") or environment.value("AILOCAL_TLS_PORT")
    return int(raw) if raw and raw.isdigit() else DEFAULT_PORT


def url(address: str) -> str:
    return f"https://{address}:{port()}"


def lan_address() -> str:
    """Return the IPv4 address of the default route's interface.

    A UDP connect selects a route and sends nothing.

    Raises:
        RuntimeError: No route, so no LAN address.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))      # TEST-NET-1: never answered
            return probe.getsockname()[0]
    except OSError as e:
        raise RuntimeError(f"no LAN address: {e}") from None


def address_error(address: str) -> str:
    """Return why the sidecar cannot serve `address`, or "" if it can."""
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return f"{address!r} is not an IPv4 address"
    if ip.is_unspecified:
        return ("0.0.0.0 is every interface, loopback included; "
                "name the LAN address with `ailocal bind lan`")
    if not any(ip in ipaddress.IPv4Network(n) for n in PERMITTED_NETWORKS):
        return (f"{address} is outside the private ranges the ailocal CA may sign "
                f"({', '.join(PERMITTED_NETWORKS)})")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((address, 0))
    except OSError:
        return f"{address} is not an address of this machine"
    return ""


def hostname() -> str:
    """Return this machine's mDNS name (`<LocalHostName>.local`), or ""."""
    from .primitives import command_output
    name = command_output("scutil", "--get", "LocalHostName", timeout=5) \
        or socket.gethostname().split(".")[0]
    return f"{name}.local" if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", name) else ""


def _private_file(path: Path) -> None:
    """Create `path` empty at 0600 so openssl never writes a readable key."""
    path.unlink(missing_ok=True)
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))


def _dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _openssl(*args: str | int | Path) -> None:
    """Run openssl; its stderr never carries key material.

    Raises:
        RuntimeError: openssl is absent or failed.
    """
    exe = shutil.which("openssl")
    if not exe:
        raise RuntimeError("TLS_OPENSSL_MISSING: openssl is not on PATH")
    r = subprocess.run([exe, *map(str, args)], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    if r.returncode:
        raise RuntimeError(f"TLS_ISSUE_FAILED: openssl {args[0]}: {r.stderr.strip()[-300:]}")


def _new_key(path: Path) -> None:
    _private_file(path)
    _openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
             "-pkeyopt", "ec_param_enc:named_curve", "-out", path)
    path.chmod(0o600)


def _config(path: Path, common_name: str, extensions: str) -> None:
    path.write_text("[req]\ndistinguished_name = dn\nprompt = no\n"
                    f"[dn]\nO = ailocal\nCN = {common_name}\n[ext]\n{extensions}\n",
                    encoding="utf-8")


def _issue_ca() -> None:
    """Create the CA key and a self-signed, name-constrained CA certificate."""
    folder = _dir(ca_cert().parent)
    _new_key(ca_key())
    permitted = ", ".join(f"permitted;IP:{n.network_address}/{n.netmask}"
                          for n in map(ipaddress.IPv4Network, PERMITTED_NETWORKS))
    cnf = folder / "ca.cnf"
    _config(cnf, f"ailocal local CA {hostname() or socket.gethostname()} {time.strftime('%Y-%m-%d')}",
            "basicConstraints = critical, CA:TRUE, pathlen:0\n"
            "keyUsage = critical, keyCertSign, cRLSign\n"
            "subjectKeyIdentifier = hash\n"
            f"nameConstraints = critical, {permitted}, permitted;DNS:local")
    try:
        _openssl("req", "-x509", "-new", "-key", ca_key(), "-sha256", "-days", CA_DAYS,
                 "-set_serial", f"0x{secrets.token_hex(16)}", "-config", cnf,
                 "-extensions", "ext", "-out", ca_cert())
    finally:
        cnf.unlink(missing_ok=True)
    ca_cert().chmod(0o644)


def issue_server(address: str) -> None:
    """Sign a server certificate whose SANs are `address` and the mDNS name."""
    folder = _dir(server_cert().parent)
    _new_key(server_key())
    names = f"IP:{address}" + (f", DNS:{h}" if (h := hostname()) else "")
    cnf, csr = folder / "server.cnf", folder / "server.csr"
    # A CN with a space is never read as a host name by name-constraint checks.
    _config(cnf, "ailocal proxy",
            "basicConstraints = critical, CA:FALSE\n"
            "keyUsage = critical, digitalSignature\n"
            "extendedKeyUsage = serverAuth\n"
            "subjectKeyIdentifier = hash\n"
            "authorityKeyIdentifier = keyid\n"
            f"subjectAltName = {names}")
    try:
        _openssl("req", "-new", "-key", server_key(), "-config", cnf, "-out", csr)
        _openssl("x509", "-req", "-in", csr, "-CA", ca_cert(), "-CAkey", ca_key(),
                 "-set_serial", f"0x{secrets.token_hex(16)}", "-days", SERVER_DAYS,
                 "-sha256", "-extfile", cnf, "-extensions", "ext", "-out", server_cert())
    finally:
        cnf.unlink(missing_ok=True)
        csr.unlink(missing_ok=True)
    server_cert().chmod(0o644)


def ensure(address: str) -> list[str]:
    """Create the CA if absent and (re)issue the server certificate if needed.

    The CA is reused, so client machines keep trusting a reissued certificate.
    Returns what was done.

    Raises:
        RuntimeError: openssl is absent or failed.
    """
    done = []
    _dir(root())
    if not (ca_cert().is_file() and ca_key().is_file()):
        _issue_ca()
        done.append(f"created a local CA; install {ca_cert()} on each client machine")
    if problems(address):
        issue_server(address)
        done.append(f"issued a {SERVER_DAYS}-day server certificate for {address}")
    return done


def handshake(address: str) -> dict:
    """Complete an in-memory TLS handshake as a client trusting only the CA.

    Proves chain, name constraints, SAN and validity with the files the
    sidecar loads. Returns the verified peer certificate.

    Raises:
        ssl.SSLError: Verification failed.
        OSError: A file is missing or unreadable.
    """
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(server_cert(), server_key())
    client = ssl.create_default_context(cafile=str(ca_cert()))
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    c = client.wrap_bio(c_in, c_out, server_hostname=address)
    s = server.wrap_bio(s_in, s_out, server_side=True)
    for _ in range(8):
        finished = 0
        for side in (c, s):
            try:
                side.do_handshake()
                finished += 1
            except ssl.SSLWantReadError:
                pass
        s_in.write(c_out.read())
        c_in.write(s_out.read())
        if finished == 2:
            return c.getpeercert() or {}
    raise ssl.SSLError("TLS handshake did not complete")


def problems(address: str) -> list[str]:
    """Return every reason the material cannot serve `address`; [] if none."""
    out = []
    for path in (ca_key(), server_key()):
        if not path.is_file():
            out.append(f"{path} is missing")
        elif path.stat().st_mode & 0o777 != 0o600:
            out.append(f"{path} is mode {path.stat().st_mode & 0o777:o}, not 600")
    for path in (ca_cert(), server_cert()):
        if not path.is_file():
            out.append(f"{path} is missing")
    if out:
        return out
    try:
        peer = handshake(address)
    except (ssl.SSLError, OSError) as e:
        return [f"the certificate does not verify for {address} against the ailocal CA: "
                f"{getattr(e, 'verify_message', '') or e}"]
    left = ssl.cert_time_to_seconds(str(peer.get("notAfter"))) - time.time()
    if left < RENEW_SECONDS:
        out.append(f"the server certificate expires in {max(0, int(left // 86400))} days")
    return out


def probe(address: str, listen_port: int | None = None, connect: str | None = None,
          timeout: float = 5) -> str:
    """Return "" if `address` serves TLS the ailocal CA verifies, else why.

    `connect` overrides the host dialled, for a listener reached another way.
    """
    try:
        context = ssl.create_default_context(cafile=str(ca_cert()))
        with socket.create_connection((connect or address, listen_port or port()), timeout=timeout) as raw, \
                context.wrap_socket(raw, server_hostname=address):
            return ""
    except (ssl.SSLError, OSError) as e:
        return f"{type(e).__name__}: {getattr(e, 'verify_message', '') or e}"


def serves_plain_http(host: str, listen_port: int, timeout: float = 5) -> bool:
    """Return whether `host:listen_port` answers a cleartext HTTP request."""
    try:
        with socket.create_connection((host, listen_port), timeout=timeout) as s:
            s.sendall(b"GET /health/liveliness HTTP/1.0\r\nHost: ailocal\r\n\r\n")
            return s.recv(5) == b"HTTP/"
    except OSError:
        return False
