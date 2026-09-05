"""TLS helpers for adding a provider: certificate matching and a live chain probe.

Both subcommands run from the repository root::

    PYTHONPATH=src .venv/bin/python -m cli.tls_probe match CERT.pem [CERT.pem ...] \\
        [--certs-dir certs] [--proxy-ca-dir proxy-ca] [--provider NAME]
    PYTHONPATH=src .venv/bin/python -m cli.tls_probe probe --host HOST [--port 443] \\
        [--cafile certs/BUNDLE.pem]

``match`` splits the given PEM files into certificates, fingerprints each
one (SHA-256 over the DER encoding) and compares the set with every
``*.pem`` bundle in ``certs/``. When one bundle already contains every
input certificate it prints ``REUSE certs/<name>`` (exit 0); otherwise it
prints the ``cat`` command that would create a new bundle (exit 10) -- it
never writes files itself. An input that cannot be read or parsed exits 2.
Inputs that live under ``proxy-ca/`` (the forward-proxy MITM CA directory,
``settings.ProxySettings.ca_dir``) get a warning: copy from there, never
write there.

``probe`` performs one TLS handshake with exactly the trust store the
router would use for the provider: ``httpx.create_ssl_context`` over
``services.http_transport.build_upstream_verify``, the same pair every
provider transport goes through, and with ``trust_env=False`` like that
transport -- so ``SSL_CERT_FILE``/``SSL_CERT_DIR`` in the operator's shell
cannot make the probe answer for a trust store the service does not have.
The verdict therefore predicts what the running service will see -- certifi
without ``--cafile``, the bundle alone with it. A bare
``ssl.create_default_context()`` is deliberately not used: its roots depend
on the interpreter build, not on what the router trusts.

Verdicts and exit codes (:class:`Verdict`). Nothing meaningful uses 1, the
status Python gives an uncaught exception, so a crash cannot be read as a
verdict:

* ``CHAIN_OK_HOSTNAME_OK`` 0 -- nothing to configure beyond ``ca_bundle``;
* ``BUNDLE_UNUSABLE`` 2 -- ``--cafile`` holds no usable certificate, so no
  handshake was attempted (the error code ``match`` also uses);
* ``CHAIN_OK_HOSTNAME_MISMATCH`` 11 -- the chain verifies but the leaf does
  not cover the host name: the only case for ``tls_verify_hostname: false``;
* ``CHAIN_FAIL`` 12 -- the chain does not verify against this trust store
  (wrong or incomplete certificates);
* ``CONNECT_FAIL`` 13 -- no TLS session at all (DNS, network, VPN, port, or
  a TLS failure that is not a verification failure).
"""

from __future__ import annotations

import argparse
import hashlib
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
from cryptography.hazmat.primitives import serialization

from services.http_transport import build_upstream_verify

EXIT_MATCH_REUSE = 0
EXIT_MATCH_ERROR = 2
# Not 1: that is the status of an uncaught exception, and the skill reads
# this code as "create a new bundle" (SKILL.md step 4).
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

    The failure verdicts start at 11 so that 1 -- what Python exits with on
    an uncaught exception -- can never be mistaken for the mismatch verdict
    that alone justifies ``tls_verify_hostname: false``.
    """

    CHAIN_OK_HOSTNAME_OK = 0
    BUNDLE_UNUSABLE = EXIT_MATCH_ERROR
    CHAIN_OK_HOSTNAME_MISMATCH = 11
    CHAIN_FAIL = 12
    CONNECT_FAIL = 13


_VERDICT_HINTS: Mapping[Verdict, str] = {
    Verdict.CHAIN_OK_HOSTNAME_OK: (
        "chain and host name verify with this trust store: keep tls_verify_hostname "
        "at its default (true)"
    ),
    Verdict.BUNDLE_UNUSABLE: (
        "the --cafile bundle could not be loaded, no handshake was attempted: check "
        "that the file holds PEM CERTIFICATE blocks (openssl x509 -in FILE -noout -subject)"
    ),
    Verdict.CHAIN_OK_HOSTNAME_MISMATCH: (
        "chain verifies, leaf SAN does not cover the host: the only case for "
        "tls_verify_hostname: false (add a dated comment with the SAN and the "
        "condition for removing the flag)"
    ),
    Verdict.CHAIN_FAIL: (
        "chain does not verify with this trust store: wrong or incomplete CA "
        "certificates for this host -- do not proceed"
    ),
    Verdict.CONNECT_FAIL: (
        "no TLS session: check DNS, VPN, port and firewall with curl -v first"
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
    """Extract every ``CERTIFICATE`` PEM block from a text.

    Text between blocks (bag attributes, comments) and PEM blocks of other
    types (private keys) are ignored.

    Args:
        text: contents of a PEM file or bundle.

    Returns:
        The certificate blocks in file order, delimiters included.
    """
    return _PEM_CERTIFICATE_BLOCK.findall(text)


def sha256_fingerprint(der: bytes) -> str:
    """Format the SHA-256 fingerprint of a DER-encoded certificate.

    Args:
        der: the certificate in DER encoding.

    Returns:
        The digest as colon-separated upper-case hex pairs, the way
        ``openssl x509 -fingerprint -sha256`` prints it.
    """
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))


def summarize_certificate(certificate: x509.Certificate) -> CertificateSummary:
    """Extract fingerprint, names and expiry from a parsed certificate.

    Args:
        certificate: the parsed certificate.

    Returns:
        Its summary.
    """
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = tuple(san.value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        dns_names = ()
    return CertificateSummary(
        fingerprint=sha256_fingerprint(certificate.public_bytes(serialization.Encoding.DER)),
        subject=certificate.subject.rfc4514_string(),
        issuer=certificate.issuer.rfc4514_string(),
        not_after=certificate.not_valid_after_utc.date().isoformat(),
        dns_names=dns_names,
    )


def load_certificates(path: Path) -> list[x509.Certificate]:
    """Load every certificate from a PEM file (a bundle may hold several).

    Args:
        path: the PEM file.

    Returns:
        The parsed certificates in file order; empty when the file has no
        ``CERTIFICATE`` block.
    """
    return [
        x509.load_pem_x509_certificate(block.encode("ascii"))
        for block in split_pem_certificates(path.read_text(encoding="utf-8"))
    ]


def fingerprint_set(certificates: Iterable[x509.Certificate]) -> frozenset[str]:
    """Collect the SHA-256 fingerprints of the given certificates.

    Args:
        certificates: parsed certificates.

    Returns:
        Their fingerprints as a set.
    """
    return frozenset(summarize_certificate(certificate).fingerprint for certificate in certificates)


def bundle_fingerprints(certs_dir: Path) -> dict[str, frozenset[str]]:
    """Fingerprint every ``*.pem`` bundle in the router's certificate directory.

    Args:
        certs_dir: the directory ``ca_bundle`` values are resolved against.

    Returns:
        Bundle file name -> fingerprints of the certificates it contains.
    """
    return {
        bundle.name: fingerprint_set(load_certificates(bundle))
        for bundle in sorted(certs_dir.glob(_BUNDLE_GLOB))
    }


def find_reusable_bundle(
    wanted: frozenset[str], bundles: Mapping[str, frozenset[str]]
) -> str | None:
    """Pick the bundle that already contains every wanted certificate.

    When several bundles qualify, the smallest one wins (the closest to an
    exact match), then the alphabetically first name.

    Args:
        wanted: fingerprints of the input certificates.
        bundles: bundle name -> fingerprints, from :func:`bundle_fingerprints`.

    Returns:
        The bundle file name, or ``None`` when no bundle covers the input.
    """
    candidates = [name for name, fingerprints in bundles.items() if wanted <= fingerprints]
    if not candidates:
        return None
    return min(candidates, key=lambda name: (len(bundles[name]), name))


def is_inside_directory(path: Path, directory: Path) -> bool:
    """Check whether ``path`` lives under ``directory`` (after resolving both).

    Args:
        path: the file to test.
        directory: the directory to test against; need not exist.

    Returns:
        True when the resolved path is inside the resolved directory.
    """
    return path.resolve().is_relative_to(directory.resolve())


def run_match(
    cert_paths: Sequence[Path], certs_dir: Path, proxy_ca_dir: Path, provider: str
) -> int:
    """Compare the input certificates with the bundles in ``certs_dir``.

    Args:
        cert_paths: PEM files given by the user (each may hold several
            certificates).
        certs_dir: the router's certificate directory.
        proxy_ca_dir: the forward-proxy CA directory; inputs under it only
            produce a warning.
        provider: provider name used in the suggested bundle file name.

    Returns:
        ``EXIT_MATCH_REUSE`` when an existing bundle covers the input,
        ``EXIT_MATCH_NEW_BUNDLE`` when a new bundle is needed (the command
        is printed, not executed), ``EXIT_MATCH_ERROR`` when an input file
        holds no certificate or cannot be read and parsed.
    """
    wanted: set[str] = set()
    for cert_path in cert_paths:
        try:
            certificates = load_certificates(cert_path)
        except (OSError, UnicodeDecodeError, ValueError) as input_error:
            print(
                f"ERROR: cannot read certificates from {cert_path}: {input_error}",
                file=sys.stderr,
            )
            return EXIT_MATCH_ERROR
        if not certificates:
            print(f"ERROR: no CERTIFICATE block in {cert_path}", file=sys.stderr)
            return EXIT_MATCH_ERROR
        if is_inside_directory(cert_path, proxy_ca_dir):
            print(
                f"WARNING: {cert_path} lives under {proxy_ca_dir}/ -- the forward-proxy "
                "MITM CA directory (settings.ProxySettings.ca_dir): copy from it, never "
                "write into it or delete its rootCA*.pem"
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
        print(f"ERROR: cannot read the bundles in {certs_dir}/: {bundle_error}", file=sys.stderr)
        return EXIT_MATCH_ERROR
    inventory = ", ".join(f"{name} ({len(prints)})" for name, prints in bundles.items())
    print(f"bundles in {certs_dir}/: {inventory or '<none>'}")
    reusable = find_reusable_bundle(frozenset(wanted), bundles)
    if reusable is not None:
        print(f"REUSE {certs_dir / reusable}")
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

    Args:
        cafile: the provider's ``ca_bundle`` path, or ``None`` for the
            router's default trust store (certifi via httpx).
        tls_verify_hostname: the provider's ``tls_verify_hostname`` value.

    Returns:
        The context, built by the same two functions the provider
        transports use.

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
    """Complete one TLS handshake and return the server's leaf certificate.

    Args:
        host: server name (also sent as SNI).
        port: TCP port.
        context: the verifying SSL context.

    Returns:
        Summary of the leaf certificate presented by the server.

    Raises:
        ssl.SSLCertVerificationError: the context rejected the certificate.
        OSError: connection, timeout or non-verification TLS failure.
    """
    with (
        socket.create_connection((host, port), timeout=_SOCKET_TIMEOUT_S) as tcp_socket,
        context.wrap_socket(tcp_socket, server_hostname=host) as tls_socket,
    ):
        der = tls_socket.getpeercert(binary_form=True)
    if der is None:
        # CERT_REQUIRED guarantees a peer certificate once the handshake
        # completed; this only narrows the Optional for the type checker.
        raise ssl.SSLError("handshake completed without a peer certificate")
    return summarize_certificate(x509.load_der_x509_certificate(der))


def _confirm_chain_without_hostname(
    host: str, port: int, context: ssl.SSLContext, hostname_detail: str
) -> ProbeResult:
    """Tell a host-name mismatch apart from a chain that does not verify.

    Runs the chain-only handshake the router performs with
    ``tls_verify_hostname: false``: when it succeeds, the strict handshake
    failed on the leaf's names alone.

    Args:
        host: server name.
        port: TCP port.
        context: the chain-only context.
        hostname_detail: verification message of the strict handshake.

    Returns:
        ``CHAIN_OK_HOSTNAME_MISMATCH`` when only the name failed, otherwise
        the failure of this handshake.
    """
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

    A strict handshake (chain + host name) runs first. When it fails only on
    the host name, a second handshake with the chain-only context the router
    uses for ``tls_verify_hostname: false`` tells a hostname mismatch apart
    from a broken chain.

    Args:
        host: server name.
        port: TCP port.
        cafile: the provider's CA bundle, or ``None`` for the default store.

    Returns:
        The verdict with the leaf certificate (when a handshake completed)
        and the error detail (when one failed).
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
    """Print the probe verdict, the trust store used and the leaf identity.

    Args:
        host: server name that was probed.
        port: TCP port that was probed.
        cafile: the CA bundle used, or ``None`` for the default store.
        result: the probe outcome.
    """
    trust_store = (
        str(cafile)
        if cafile is not None
        else "httpx default: certifi; SSL_CERT_FILE/SSL_CERT_DIR are ignored, as in "
        "the router's transport (what the router uses without ca_bundle)"
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
    """Build the command-line parser with the ``match`` and ``probe`` subcommands.

    Returns:
        The configured parser.
    """
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
    """Run the ``match`` or ``probe`` subcommand.

    Args:
        argv: command-line arguments without the program name; ``None`` --
            ``sys.argv[1:]``.

    Returns:
        The process exit code: the ``EXIT_MATCH_*`` constants for ``match``,
        the :class:`Verdict` value for ``probe``.
    """
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
