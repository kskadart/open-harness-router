"""TLS helpers for adding a provider: certificate matching and a live chain probe.

Both subcommands run from the repository root::

    PYTHONPATH=src .venv/bin/python -m cli.tls_probe match CERT.pem [CERT.pem ...] \\
        [--certs-dir certs] [--proxy-ca-dir proxy-ca] [--provider NAME]
    PYTHONPATH=src .venv/bin/python -m cli.tls_probe probe --host HOST [--port 443] \\
        [--cafile certs/BUNDLE.pem]

``match`` fingerprints the input certificates and compares them with every
``*.pem`` bundle in ``certs/``: ``REUSE certs/<name>`` (exit 0) or the ``cat``
command that would build a new bundle (exit 10) -- it never writes files, and
warns about inputs under ``proxy-ca/`` and about certificates a reused bundle
carries beyond the input.

``probe`` handshakes once with exactly the trust store the router would use
for the provider (``httpx.create_ssl_context`` over ``build_upstream_verify``,
``trust_env=False`` as in the transport), so its verdict predicts what the
running service will see. Verdicts and exit codes: :class:`Verdict`.
"""

from __future__ import annotations

import argparse
import re
import socket
import ssl
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes

from services.http_transport import build_upstream_verify

EXIT_MATCH_REUSE = 0
EXIT_MATCH_ERROR = 2
# The skill reads this code as "create a new bundle" (SKILL.md step 4).
EXIT_MATCH_NEW_BUNDLE = 10

DEFAULT_CERTS_DIR = Path("certs")
DEFAULT_PROXY_CA_DIR = Path("proxy-ca")
DEFAULT_TLS_PORT = 443
DEFAULT_PROVIDER_PLACEHOLDER = "<provider>"

_SOCKET_TIMEOUT_S = 10.0
_BUNDLE_GLOB = "*.pem"
_PEM_CERTIFICATE_BLOCK = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL
)
# OpenSSL X509_V_ERR_HOSTNAME_MISMATCH / X509_V_ERR_IP_ADDRESS_MISMATCH: the
# chain itself verified, only the leaf does not cover the requested name.
_HOSTNAME_MISMATCH_VERIFY_CODES = frozenset({62, 64})


class Verdict(IntEnum):
    """Probe verdicts; the integer value doubles as the process exit code.

    Failures start at 11 so that 1, Python's uncaught-exception status, can
    never be read as a verdict.

    * ``CHAIN_OK_HOSTNAME_OK`` 0 -- chain and host name verify;
    * ``BUNDLE_UNUSABLE`` 2 -- ``--cafile`` holds no usable certificate;
    * ``CHAIN_OK_HOSTNAME_MISMATCH`` 11 -- chain verifies, leaf SAN does not
      cover the host;
    * ``CHAIN_FAIL`` 12 -- the chain does not verify with this trust store;
    * ``CONNECT_FAIL`` 13 -- no TLS session at all (DNS, port, VPN).
    """

    CHAIN_OK_HOSTNAME_OK = 0
    BUNDLE_UNUSABLE = EXIT_MATCH_ERROR
    CHAIN_OK_HOSTNAME_MISMATCH = 11
    CHAIN_FAIL = 12
    CONNECT_FAIL = 13


_VERDICT_HINTS: Mapping[Verdict, str] = {
    Verdict.CHAIN_OK_HOSTNAME_OK: (
        "nothing to fix: set ca_bundle, leave tls_verify_hostname default"
    ),
    Verdict.BUNDLE_UNUSABLE: (
        "--cafile holds no certificate; check with 'openssl x509 -in FILE -noout -subject'"
    ),
    Verdict.CHAIN_OK_HOSTNAME_MISMATCH: (
        "chain OK, leaf SAN does not cover the host: the only case for "
        "tls_verify_hostname: false"
    ),
    Verdict.CHAIN_FAIL: (
        "chain does not verify with this trust store; get the full chain from the "
        "gateway owner"
    ),
    Verdict.CONNECT_FAIL: (
        "no TLS session; check DNS/port/VPN with 'curl -v https://HOST:PORT'"
    ),
}


@dataclass(frozen=True, slots=True)
class CertificateSummary:
    """Human-readable identity of one X.509 certificate.

    Attributes:
        fingerprint: colon-separated upper-case SHA-256 over the DER encoding.
        subject: RFC 4514 subject string.
        issuer: RFC 4514 issuer string.
        not_after: expiry date (UTC, ISO 8601).
        dns_names: subjectAltName DNS entries; empty when the extension is absent.
    """

    fingerprint: str
    subject: str
    issuer: str
    not_after: str
    dns_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of a probe handshake.

    Attributes:
        verdict: classification of the handshake.
        leaf: the server's leaf certificate when a handshake completed.
        detail: the verification or connection error text, empty on success.
    """

    verdict: Verdict
    leaf: CertificateSummary | None
    detail: str


def split_pem_certificates(text: str) -> list[str]:
    """Extract every ``CERTIFICATE`` PEM block, in file order, delimiters included.

    Text between blocks (bag attributes, comments) and PEM blocks of other
    types (private keys) are ignored.
    """
    return _PEM_CERTIFICATE_BLOCK.findall(text)


def summarize_certificate(certificate: x509.Certificate) -> CertificateSummary:
    """Extract fingerprint, names and expiry from a parsed certificate."""
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = tuple(san.value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        dns_names = ()
    # Colon-separated pairs, the way `openssl x509 -fingerprint -sha256` prints it.
    digest = certificate.fingerprint(hashes.SHA256()).hex().upper()
    return CertificateSummary(
        fingerprint=":".join(digest[index : index + 2] for index in range(0, len(digest), 2)),
        subject=certificate.subject.rfc4514_string(),
        issuer=certificate.issuer.rfc4514_string(),
        not_after=certificate.not_valid_after_utc.date().isoformat(),
        dns_names=dns_names,
    )


def load_certificates(path: Path) -> list[x509.Certificate]:
    """Load every certificate from a PEM file; empty when it holds no ``CERTIFICATE``."""
    return [
        x509.load_pem_x509_certificate(block.encode("ascii"))
        for block in split_pem_certificates(path.read_text(encoding="utf-8"))
    ]


def fingerprint_set(certificates: Iterable[x509.Certificate]) -> frozenset[str]:
    """Collect the SHA-256 fingerprints of the given certificates."""
    return frozenset(summarize_certificate(certificate).fingerprint for certificate in certificates)


def bundle_fingerprints(certs_dir: Path) -> dict[str, frozenset[str]]:
    """Map each ``*.pem`` in ``certs_dir`` to the fingerprints it contains."""
    return {
        bundle.name: fingerprint_set(load_certificates(bundle))
        for bundle in sorted(certs_dir.glob(_BUNDLE_GLOB))
    }


def find_reusable_bundle(
    wanted: frozenset[str], bundles: Mapping[str, frozenset[str]]
) -> str | None:
    """Pick the bundle that already contains every wanted certificate, else ``None``.

    When several qualify, the smallest one wins (the closest to an exact
    match), then the alphabetically first name.
    """
    candidates = [name for name, fingerprints in bundles.items() if wanted <= fingerprints]
    if not candidates:
        return None
    return min(candidates, key=lambda name: (len(bundles[name]), name))


def extra_certificates(bundle: Path, wanted: frozenset[str]) -> list[CertificateSummary]:
    """Summarize the certificates a bundle holds beyond the wanted ones."""
    return [
        summary
        for summary in (
            summarize_certificate(certificate) for certificate in load_certificates(bundle)
        )
        if summary.fingerprint not in wanted
    ]


def _warn_about_extra_certificates(bundle: Path, wanted: frozenset[str]) -> None:
    """Name the CAs a reused bundle adds to the provider's trust store.

    Only the operator can decide whether trusting them is acceptable.
    """
    extras = extra_certificates(bundle, wanted)
    if not extras:
        return
    print(
        f"WARNING: {bundle} holds {len(extras)} certificate(s) beyond the input; reusing it "
        "makes this provider trust them as well:",
        file=sys.stderr,
    )
    for extra in extras:
        print(f"    subject={extra.subject}  notAfter={extra.not_after}", file=sys.stderr)
        print(f"    sha256={extra.fingerprint}", file=sys.stderr)


def run_match(
    cert_paths: Sequence[Path], certs_dir: Path, proxy_ca_dir: Path, provider: str
) -> int:
    """Compare the input certificates with the bundles in ``certs_dir``.

    Returns ``EXIT_MATCH_REUSE`` when a bundle covers the input,
    ``EXIT_MATCH_NEW_BUNDLE`` when the printed ``cat`` command has to build
    one, ``EXIT_MATCH_ERROR`` when an input cannot be read or holds no
    certificate.
    """
    wanted: set[str] = set()
    for cert_path in cert_paths:
        try:
            certificates = load_certificates(cert_path)
        except (OSError, UnicodeDecodeError, ValueError) as input_error:
            print(
                f"ERROR: cannot read certificates from {cert_path}: {input_error} "
                "(export the chain with 'openssl s_client -showcerts -connect HOST:443')",
                file=sys.stderr,
            )
            return EXIT_MATCH_ERROR
        if not certificates:
            print(
                f"ERROR: {cert_path}: no PEM CERTIFICATE block (DER? 'openssl x509 "
                f"-inform der -in {cert_path} -out {cert_path}.pem')",
                file=sys.stderr,
            )
            return EXIT_MATCH_ERROR
        if cert_path.resolve().is_relative_to(proxy_ca_dir.resolve()):
            print(
                f"WARNING: {cert_path} lives under {proxy_ca_dir}/ -- the forward-proxy "
                "MITM CA directory (settings.ProxySettings.ca_dir): copy from it, never "
                "write into it or delete its rootCA*.pem",
                file=sys.stderr,
            )
        for certificate in certificates:
            summary = summarize_certificate(certificate)
            print(f"{cert_path}: subject={summary.subject}")
            print(f"    issuer={summary.issuer}  notAfter={summary.not_after}")
            print(f"    sha256={summary.fingerprint}")
            wanted.add(summary.fingerprint)

    try:
        bundles = bundle_fingerprints(certs_dir)
    except (OSError, UnicodeDecodeError, ValueError) as bundle_error:
        print(
            f"ERROR: cannot read the bundles in {certs_dir}/: {bundle_error} "
            "(run from the repository root, or point --certs-dir at ROUTER_CERTS_DIR)",
            file=sys.stderr,
        )
        return EXIT_MATCH_ERROR
    inventory = ", ".join(f"{name} ({len(prints)})" for name, prints in bundles.items())
    print(f"bundles in {certs_dir}/: {inventory or '<none>'}")
    reusable = find_reusable_bundle(frozenset(wanted), bundles)
    if reusable is not None:
        reused_bundle = certs_dir / reusable
        print(f"REUSE {reused_bundle}")
        _warn_about_extra_certificates(reused_bundle, frozenset(wanted))
        return EXIT_MATCH_REUSE
    inputs = " ".join(str(cert_path) for cert_path in cert_paths)
    print(
        f"NEW bundle needed: no file in {certs_dir}/ contains all "
        f"{len(wanted)} input certificate(s); run manually:"
    )
    print(f"  cat {inputs} > {certs_dir / f'{provider}_ca.pem'}")
    return EXIT_MATCH_NEW_BUNDLE


def build_probe_context(cafile: Path | None, tls_verify_hostname: bool) -> ssl.SSLContext:
    """Build the SSL context the router itself would use for a provider.

    Raises:
        OSError: ``cafile`` cannot be read or holds no usable certificate
            (``ssl.SSLError`` is an ``OSError``).
    """
    # trust_env=False mirrors the AsyncHTTPTransport in
    # services.http_transport: without it httpx would replace certifi with
    # SSL_CERT_FILE/SSL_CERT_DIR from the shell and the probe would answer
    # for a trust store the service never uses.
    return httpx.create_ssl_context(
        verify=build_upstream_verify(cafile, tls_verify_hostname), trust_env=False
    )


def _handshake(host: str, port: int, context: ssl.SSLContext) -> CertificateSummary:
    """Complete one TLS handshake (``host`` doubles as SNI) and summarize the leaf.

    Raises:
        ssl.SSLCertVerificationError: the context rejected the certificate.
        OSError: connection, timeout or non-verification TLS failure.
    """
    with (
        socket.create_connection((host, port), timeout=_SOCKET_TIMEOUT_S) as tcp_socket,
        context.wrap_socket(tcp_socket, server_hostname=host) as tls_socket,
    ):
        der = tls_socket.getpeercert(binary_form=True)
    assert der is not None, "CERT_REQUIRED: a completed handshake always has a peer certificate"
    return summarize_certificate(x509.load_der_x509_certificate(der))


def _confirm_chain_without_hostname(
    host: str, port: int, context: ssl.SSLContext, hostname_detail: str
) -> ProbeResult:
    """Retry chain-only: a handshake that now succeeds failed on the leaf's names alone."""
    try:
        leaf = _handshake(host, port, context)
    except ssl.SSLCertVerificationError as chain_error:
        return ProbeResult(Verdict.CHAIN_FAIL, None, chain_error.verify_message)
    except OSError as connect_error:
        return ProbeResult(Verdict.CONNECT_FAIL, None, str(connect_error))
    return ProbeResult(Verdict.CHAIN_OK_HOSTNAME_MISMATCH, leaf, hostname_detail)


def probe_host(host: str, port: int, cafile: Path | None) -> ProbeResult:
    """Handshake with ``host`` using the router's trust store and classify the outcome.

    Both contexts are built before the first connection: loading a bundle
    that holds no certificate raises ``ssl.SSLError``, an ``OSError`` that
    would otherwise be caught below and reported as a connection failure.
    """
    try:
        strict_context = build_probe_context(cafile, tls_verify_hostname=True)
        chain_only_context = build_probe_context(cafile, tls_verify_hostname=False)
    except (OSError, ValueError) as bundle_error:
        return ProbeResult(
            Verdict.BUNDLE_UNUSABLE, None, f"cannot load {cafile}: {bundle_error}"
        )

    try:
        leaf = _handshake(host, port, strict_context)
    except ssl.SSLCertVerificationError as strict_error:
        if strict_error.verify_code not in _HOSTNAME_MISMATCH_VERIFY_CODES:
            return ProbeResult(Verdict.CHAIN_FAIL, None, strict_error.verify_message)
        hostname_detail = strict_error.verify_message
    except OSError as connect_error:
        return ProbeResult(Verdict.CONNECT_FAIL, None, str(connect_error))
    else:
        return ProbeResult(Verdict.CHAIN_OK_HOSTNAME_OK, leaf, "")

    return _confirm_chain_without_hostname(host, port, chain_only_context, hostname_detail)


def _print_probe_result(host: str, port: int, cafile: Path | None, result: ProbeResult) -> None:
    """Print the probe verdict, the trust store used and the leaf identity."""
    trust_store = (
        str(cafile) if cafile is not None else "certifi (httpx default, SSL_CERT_FILE ignored)"
    )
    print(f"verdict: {result.verdict.name}")
    print(f"host: {host}:{port}")
    print(f"trust store: {trust_store}")
    if result.leaf is not None:
        print(f"leaf subject: {result.leaf.subject}")
        print(f"leaf SAN: {', '.join(result.leaf.dns_names) or '<none>'}")
        print(f"leaf issuer: {result.leaf.issuer}")
        print(f"leaf notAfter: {result.leaf.not_after}")
        print(f"leaf sha256: {result.leaf.fingerprint}")
    if result.detail:
        print(f"detail: {result.detail}")
    print(f"hint: {_VERDICT_HINTS[result.verdict]}")


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser with the ``match`` and ``probe`` subcommands."""
    parser = argparse.ArgumentParser(
        prog="cli.tls_probe",
        description="Certificate matching against certs/ and a live TLS probe "
        "with the router's trust store.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    match_parser = subparsers.add_parser(
        "match", help="compare PEM files with the bundles in certs/ (never writes)"
    )
    match_parser.add_argument("certs", nargs="+", type=Path, metavar="CERT.pem")
    match_parser.add_argument(
        "--certs-dir", type=Path, default=DEFAULT_CERTS_DIR, help="router certs_dir"
    )
    match_parser.add_argument(
        "--proxy-ca-dir",
        type=Path,
        default=DEFAULT_PROXY_CA_DIR,
        help="forward-proxy CA directory; inputs under it only get a warning",
    )
    match_parser.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER_PLACEHOLDER,
        help="provider name used in the suggested bundle file name",
    )

    probe_parser = subparsers.add_parser(
        "probe", help="one TLS handshake with the trust store the router would use"
    )
    probe_parser.add_argument("--host", required=True, help="server name to probe")
    probe_parser.add_argument("--port", type=int, default=DEFAULT_TLS_PORT)
    probe_parser.add_argument(
        "--cafile", type=Path, default=None, help="the provider's ca_bundle file"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run ``match`` (``EXIT_MATCH_*``) or ``probe`` (a :class:`Verdict`) and return its code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "match":
        return run_match(args.certs, args.certs_dir, args.proxy_ca_dir, args.provider)
    if args.cafile is not None and not args.cafile.is_file():
        parser.error(f"--cafile not found: {args.cafile}")
    result = probe_host(args.host, args.port, args.cafile)
    _print_probe_result(args.host, args.port, args.cafile, result)
    return int(result.verdict)


if __name__ == "__main__":
    sys.exit(main())
