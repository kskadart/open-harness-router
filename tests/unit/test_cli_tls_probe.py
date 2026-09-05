"""Unit tests for ``cli.tls_probe``: certificate matching and the probe verdicts.

The offline certificate logic behind the ``match`` subcommand, the
SSL-context builder and the ``probe`` verdicts are exercised. The verdict
tests run real TLS handshakes against a local ``ssl`` server on 127.0.0.1
with certificates generated in the test, so nothing leaves the machine.
"""

from __future__ import annotations

import contextlib
import datetime
import ipaddress
import socket
import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from cli.tls_probe import (
    EXIT_MATCH_ERROR,
    EXIT_MATCH_NEW_BUNDLE,
    EXIT_MATCH_REUSE,
    Verdict,
    build_probe_context,
    bundle_fingerprints,
    find_reusable_bundle,
    fingerprint_set,
    load_certificates,
    main,
    probe_host,
    run_match,
    split_pem_certificates,
    summarize_certificate,
)

_TEST_CA_PEM_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "certs" / "test_ca.pem"
_SECOND_CA_COMMON_NAME = "open-harness-router-second-test-ca"
_THIRD_CA_COMMON_NAME = "open-harness-router-third-test-ca"
_TEST_CA_COMMON_NAME = "open-harness-router-test-ca"
_PROVIDER_NAME = "demo"
_FAKE_KEY_BLOCK = "-----BEGIN PRIVATE KEY-----\nbm90IGEga2V5\n-----END PRIVATE KEY-----\n"
_MALFORMED_CERTIFICATE_BLOCK = (
    "-----BEGIN CERTIFICATE-----\nnot base64 at all ***\n-----END CERTIFICATE-----\n"
)

_PROBE_CA_COMMON_NAME = "open-harness-router-probe-ca"
_PROBE_LEAF_COMMON_NAME = "open-harness-router-probe-leaf"
_PROBE_HOST = "127.0.0.1"
_UNCOVERED_HOST_NAME = "not-the-probed-host.example"
# Wake-up granularity of the accept loop, so the server thread notices the
# stop event quickly, and the cap on any single blocking server operation.
_ACCEPT_POLL_S = 0.5
_SERVER_TIMEOUT_S = 5.0


def _self_signed_ca_pem(common_name: str) -> bytes:
    """Generate a throwaway self-signed CA certificate in PEM encoding.

    Args:
        common_name: subject/issuer CN, so the certificate is recognizable
            in assertion output.

    Returns:
        The PEM bytes of the certificate.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


@dataclass(frozen=True, slots=True)
class _ProbeCertificates:
    """Files backing the probe handshake tests.

    Attributes:
        ca_bundle: the generated CA, as a provider would pass it in ``--cafile``.
        matching_chain: leaf + key whose SAN covers ``_PROBE_HOST``.
        uncovered_chain: leaf + key whose SAN covers another name only.
    """

    ca_bundle: Path
    matching_chain: Path
    uncovered_chain: Path


def _generate_ca() -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Generate a self-signed CA that passes ``VERIFY_X509_STRICT``.

    Returns:
        The CA certificate and its private key, for signing leaves.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _PROBE_CA_COMMON_NAME)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()), critical=False
        )
        .sign(private_key, hashes.SHA256())
    )
    return certificate, private_key


def _write_server_chain(
    path: Path,
    ca_certificate: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
    subject_alternative_name: x509.SubjectAlternativeName,
) -> None:
    """Write a leaf certificate plus its key, ready for ``load_cert_chain``.

    Python 3.13 verifies with ``VERIFY_X509_STRICT``, so the leaf carries a
    subject key identifier and an authority key identifier next to the SAN.

    Args:
        path: the PEM file to write (certificate first, then the key).
        ca_certificate: the issuing CA.
        ca_key: the issuing CA's private key.
        subject_alternative_name: the names the leaf covers.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.UTC)
    ca_subject_key_id = ca_certificate.extensions.get_extension_for_class(
        x509.SubjectKeyIdentifier
    ).value
    certificate = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _PROBE_LEAF_COMMON_NAME)])
        )
        .issuer_name(ca_certificate.subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(subject_alternative_name, critical=False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_subject_key_id),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    path.write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
        + private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _accept_handshakes(
    listener: socket.socket, server_context: ssl.SSLContext, stop: threading.Event
) -> None:
    """Serve TLS handshakes until ``stop`` is set or the listener is closed.

    The probe drops the connection right after the handshake and a rejected
    certificate ends it even earlier, so every socket error is expected here.

    Args:
        listener: the bound and listening socket.
        server_context: the server-side SSL context.
        stop: set by the fixture teardown to end the loop.
    """
    while not stop.is_set():
        try:
            connection, _peer = listener.accept()
        except TimeoutError:
            continue
        except OSError:
            return
        connection.settimeout(_SERVER_TIMEOUT_S)
        try:
            with server_context.wrap_socket(connection, server_side=True) as tls_connection:
                tls_connection.recv(1)
        except OSError:
            connection.close()


@contextlib.contextmanager
def _tls_server(chain_path: Path) -> Iterator[int]:
    """Run a local TLS server presenting ``chain_path`` for the duration of the block.

    Args:
        chain_path: PEM file with the leaf certificate and its private key.

    Yields:
        The port the server listens on at ``_PROBE_HOST``.
    """
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(chain_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((_PROBE_HOST, 0))
    listener.listen(2)
    listener.settimeout(_ACCEPT_POLL_S)
    stop = threading.Event()
    worker = threading.Thread(
        target=_accept_handshakes, args=(listener, server_context, stop), daemon=True
    )
    worker.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        listener.close()
        worker.join(timeout=_SERVER_TIMEOUT_S)


def _subject_common_names(context: ssl.SSLContext) -> set[str]:
    """Collect the common names of the trust anchors loaded into a context.

    Args:
        context: the SSL context to inspect.

    Returns:
        The ``commonName`` of every loaded CA certificate.
    """
    return {
        value
        for loaded in context.get_ca_certs()
        for relative_name in loaded["subject"]
        for attribute, value in relative_name
        if attribute == "commonName"
    }


@pytest.fixture
def test_ca_pem() -> bytes:
    """The repository's single-certificate test CA."""
    return _TEST_CA_PEM_PATH.read_bytes()


@pytest.fixture
def second_ca_pem() -> bytes:
    """A generated CA that shares a bundle with the test CA."""
    return _self_signed_ca_pem(_SECOND_CA_COMMON_NAME)


@pytest.fixture
def second_ca_path(tmp_path: Path, second_ca_pem: bytes) -> Path:
    """The second CA written as a user-supplied input file outside certs/."""
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    path = input_dir / "second_ca.pem"
    path.write_bytes(second_ca_pem)
    return path


@pytest.fixture
def third_ca_path(tmp_path: Path) -> Path:
    """A generated CA that no bundle in certs/ contains."""
    path = tmp_path / "third_ca.pem"
    path.write_bytes(_self_signed_ca_pem(_THIRD_CA_COMMON_NAME))
    return path


@pytest.fixture
def certs_dir(tmp_path: Path, test_ca_pem: bytes, second_ca_pem: bytes) -> Path:
    """A certs/ directory with a one-certificate bundle and a two-certificate bundle."""
    directory = tmp_path / "certs"
    directory.mkdir()
    (directory / "single_ca.pem").write_bytes(test_ca_pem)
    (directory / "pair_bundle.pem").write_bytes(test_ca_pem + second_ca_pem)
    return directory


@pytest.fixture
def proxy_ca_dir(tmp_path: Path) -> Path:
    """The forward-proxy CA directory the matcher must warn about."""
    directory = tmp_path / "proxy-ca"
    directory.mkdir()
    return directory


@pytest.fixture(scope="module")
def probe_certificates(tmp_path_factory: pytest.TempPathFactory) -> _ProbeCertificates:
    """A CA and two server chains for the handshake tests (generated once)."""
    directory = tmp_path_factory.mktemp("probe-certs")
    ca_certificate, ca_key = _generate_ca()
    ca_bundle = directory / "probe_ca.pem"
    ca_bundle.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))
    matching_chain = directory / "matching_chain.pem"
    _write_server_chain(
        matching_chain,
        ca_certificate,
        ca_key,
        x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(_PROBE_HOST))]),
    )
    uncovered_chain = directory / "uncovered_chain.pem"
    _write_server_chain(
        uncovered_chain,
        ca_certificate,
        ca_key,
        x509.SubjectAlternativeName([x509.DNSName(_UNCOVERED_HOST_NAME)]),
    )
    return _ProbeCertificates(ca_bundle, matching_chain, uncovered_chain)


@pytest.fixture
def matching_server(probe_certificates: _ProbeCertificates) -> Iterator[int]:
    """Port of a local server whose leaf certificate covers ``_PROBE_HOST``."""
    with _tls_server(probe_certificates.matching_chain) as port:
        yield port


@pytest.fixture
def uncovered_server(probe_certificates: _ProbeCertificates) -> Iterator[int]:
    """Port of a local server whose leaf certificate covers another name only."""
    with _tls_server(probe_certificates.uncovered_chain) as port:
        yield port


@pytest.fixture
def closed_port() -> int:
    """A port on ``_PROBE_HOST`` that was bound and released, so nothing listens on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as released:
        released.bind((_PROBE_HOST, 0))
        return int(released.getsockname()[1])


@pytest.fixture
def empty_bundle(tmp_path: Path) -> Path:
    """A ``--cafile`` that exists but holds no certificate."""
    path = tmp_path / "no_certificate.pem"
    path.write_text(_FAKE_KEY_BLOCK, encoding="utf-8")
    return path


def test_split_pem_certificates_bundle_with_two_blocks_returns_both(
    test_ca_pem: bytes, second_ca_pem: bytes
) -> None:
    """Two concatenated certificates with text in between come back as two blocks."""
    bundle_text = test_ca_pem.decode() + "# bag attributes\n" + second_ca_pem.decode()

    blocks = split_pem_certificates(bundle_text)

    assert len(blocks) == 2
    assert all(block.startswith("-----BEGIN CERTIFICATE-----") for block in blocks)
    assert all(block.endswith("-----END CERTIFICATE-----") for block in blocks)


def test_split_pem_certificates_private_key_block_is_ignored(test_ca_pem: bytes) -> None:
    """A PRIVATE KEY block next to a certificate is not returned as a certificate."""
    blocks = split_pem_certificates(_FAKE_KEY_BLOCK + test_ca_pem.decode())

    assert len(blocks) == 1


def test_summarize_certificate_fingerprint_matches_cryptography_sha256(
    test_ca_pem: bytes,
) -> None:
    """The colon-separated fingerprint equals cryptography's own SHA-256 digest."""
    certificate = x509.load_pem_x509_certificate(test_ca_pem)
    digest = certificate.fingerprint(hashes.SHA256()).hex().upper()
    expected = ":".join(digest[index : index + 2] for index in range(0, len(digest), 2))

    summary = summarize_certificate(certificate)

    assert summary.fingerprint == expected
    assert summary.subject == "CN=open-harness-router-test-ca"


def test_find_reusable_bundle_input_only_in_pair_bundle_returns_pair_bundle(
    certs_dir: Path, second_ca_path: Path
) -> None:
    """An input present in exactly one bundle selects that bundle."""
    bundles = bundle_fingerprints(certs_dir)
    wanted = fingerprint_set(load_certificates(second_ca_path))

    assert find_reusable_bundle(wanted, bundles) == "pair_bundle.pem"


def test_find_reusable_bundle_input_in_two_bundles_prefers_the_smaller(
    certs_dir: Path,
) -> None:
    """When both bundles cover the input, the one closest to an exact match wins."""
    bundles = bundle_fingerprints(certs_dir)
    wanted = fingerprint_set(load_certificates(_TEST_CA_PEM_PATH))

    assert find_reusable_bundle(wanted, bundles) == "single_ca.pem"


def test_find_reusable_bundle_no_bundle_covers_all_inputs_returns_none(
    certs_dir: Path, third_ca_path: Path
) -> None:
    """A set with a certificate no bundle holds is not reusable, even if partly covered."""
    bundles = bundle_fingerprints(certs_dir)
    wanted = fingerprint_set(
        load_certificates(_TEST_CA_PEM_PATH) + load_certificates(third_ca_path)
    )

    assert find_reusable_bundle(wanted, bundles) is None


def test_run_match_inputs_covered_by_bundle_prints_reuse_and_returns_0(
    certs_dir: Path, second_ca_path: Path, proxy_ca_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Root + sub CA given separately, both in one bundle -> REUSE that bundle."""
    exit_code = run_match(
        [_TEST_CA_PEM_PATH, second_ca_path], certs_dir, proxy_ca_dir, _PROVIDER_NAME
    )

    captured = capsys.readouterr()
    assert exit_code == EXIT_MATCH_REUSE
    assert f"REUSE {certs_dir / 'pair_bundle.pem'}" in captured.out
    assert "WARNING" not in captured.out


def test_run_match_no_bundle_covers_inputs_prints_cat_command_and_returns_10(
    certs_dir: Path, third_ca_path: Path, proxy_ca_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown CA yields the cat command for a new bundle, which is not executed."""
    new_bundle = certs_dir / f"{_PROVIDER_NAME}_ca.pem"

    exit_code = run_match([third_ca_path], certs_dir, proxy_ca_dir, _PROVIDER_NAME)

    captured = capsys.readouterr()
    assert exit_code == EXIT_MATCH_NEW_BUNDLE
    assert f"cat {third_ca_path} > {new_bundle}" in captured.out
    assert not new_bundle.exists()


def test_run_match_input_under_proxy_ca_dir_prints_warning(
    certs_dir: Path, proxy_ca_dir: Path, test_ca_pem: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    """A certificate read from proxy-ca/ still matches but is flagged as a foreign directory."""
    foreign_input = proxy_ca_dir / "rt-root.pem"
    foreign_input.write_bytes(test_ca_pem)

    exit_code = run_match([foreign_input], certs_dir, proxy_ca_dir, _PROVIDER_NAME)

    captured = capsys.readouterr()
    assert exit_code == EXIT_MATCH_REUSE
    assert f"WARNING: {foreign_input} lives under {proxy_ca_dir}/" in captured.out


def test_run_match_input_without_certificate_block_returns_2(
    tmp_path: Path, certs_dir: Path, proxy_ca_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file with no CERTIFICATE block is an error, not an empty match."""
    empty_input = tmp_path / "empty.pem"
    empty_input.write_text(_FAKE_KEY_BLOCK, encoding="utf-8")

    exit_code = run_match([empty_input], certs_dir, proxy_ca_dir, _PROVIDER_NAME)

    captured = capsys.readouterr()
    assert exit_code == EXIT_MATCH_ERROR
    assert f"no CERTIFICATE block in {empty_input}" in captured.err


def test_run_match_missing_input_file_returns_2_without_traceback(
    tmp_path: Path, certs_dir: Path, proxy_ca_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mistyped path is an input error, not an uncaught FileNotFoundError."""
    missing_input = tmp_path / "typo.pem"

    exit_code = run_match([missing_input], certs_dir, proxy_ca_dir, _PROVIDER_NAME)

    captured = capsys.readouterr()
    assert exit_code == EXIT_MATCH_ERROR
    assert f"cannot read certificates from {missing_input}" in captured.err


def test_run_match_malformed_input_pem_returns_2_without_traceback(
    tmp_path: Path, certs_dir: Path, proxy_ca_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CERTIFICATE block with garbage inside is reported, not raised."""
    malformed_input = tmp_path / "malformed.pem"
    malformed_input.write_text(_MALFORMED_CERTIFICATE_BLOCK, encoding="utf-8")

    exit_code = run_match([malformed_input], certs_dir, proxy_ca_dir, _PROVIDER_NAME)

    captured = capsys.readouterr()
    assert exit_code == EXIT_MATCH_ERROR
    assert f"cannot read certificates from {malformed_input}" in captured.err


def test_run_match_malformed_bundle_in_certs_dir_returns_2_without_traceback(
    certs_dir: Path, second_ca_path: Path, proxy_ca_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken *.pem in certs/ names the directory it came from instead of crashing."""
    (certs_dir / "malformed_ca.pem").write_text(_MALFORMED_CERTIFICATE_BLOCK, encoding="utf-8")

    exit_code = run_match([second_ca_path], certs_dir, proxy_ca_dir, _PROVIDER_NAME)

    captured = capsys.readouterr()
    assert exit_code == EXIT_MATCH_ERROR
    assert f"cannot read the bundles in {certs_dir}" in captured.err


def test_main_match_subcommand_forwards_directories_to_run_match(
    certs_dir: Path, second_ca_path: Path, proxy_ca_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The argparse front end reaches the same REUSE decision as the direct call."""
    exit_code = main(
        [
            "match",
            str(second_ca_path),
            "--certs-dir",
            str(certs_dir),
            "--proxy-ca-dir",
            str(proxy_ca_dir),
        ]
    )

    assert exit_code == EXIT_MATCH_REUSE
    assert f"REUSE {certs_dir / 'pair_bundle.pem'}" in capsys.readouterr().out


def test_verdict_values_are_the_documented_exit_codes() -> None:
    """The skill documents these codes; the enum is the single source of truth."""
    assert int(Verdict.CHAIN_OK_HOSTNAME_OK) == 0
    assert int(Verdict.BUNDLE_UNUSABLE) == 2
    assert int(Verdict.CHAIN_OK_HOSTNAME_MISMATCH) == 11
    assert int(Verdict.CHAIN_FAIL) == 12
    assert int(Verdict.CONNECT_FAIL) == 13


def test_documented_exit_codes_never_use_1() -> None:
    """Exit 1 stays Python's uncaught-exception status, so no verdict may claim it."""
    codes = {EXIT_MATCH_REUSE, EXIT_MATCH_NEW_BUNDLE, EXIT_MATCH_ERROR}
    codes.update(int(verdict) for verdict in Verdict)

    assert 1 not in codes


def test_build_probe_context_with_cafile_trusts_only_that_bundle() -> None:
    """With a bundle the context holds exactly its roots and checks the host name."""
    context = build_probe_context(_TEST_CA_PEM_PATH, tls_verify_hostname=True)

    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert len(context.get_ca_certs()) == 1


def test_build_probe_context_hostname_off_keeps_chain_verification() -> None:
    """tls_verify_hostname=false skips only the name match, never the chain."""
    context = build_probe_context(_TEST_CA_PEM_PATH, tls_verify_hostname=False)

    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert len(context.get_ca_certs()) == 1


def test_build_probe_context_without_cafile_ignores_ssl_cert_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default store is the router's (certifi), not the shell's SSL_CERT_FILE."""
    monkeypatch.setenv("SSL_CERT_FILE", str(_TEST_CA_PEM_PATH))

    context = build_probe_context(None, tls_verify_hostname=True)

    common_names = _subject_common_names(context)
    assert _TEST_CA_COMMON_NAME not in common_names
    assert len(common_names) > 1


def test_probe_host_chain_and_hostname_valid_returns_verdict_0(
    probe_certificates: _ProbeCertificates, matching_server: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """A leaf issued by the given bundle and covering the host is the clean case."""
    result = probe_host(_PROBE_HOST, matching_server, probe_certificates.ca_bundle)
    exit_code = main(
        [
            "probe",
            "--host",
            _PROBE_HOST,
            "--port",
            str(matching_server),
            "--cafile",
            str(probe_certificates.ca_bundle),
        ]
    )

    captured = capsys.readouterr()
    assert result.verdict is Verdict.CHAIN_OK_HOSTNAME_OK
    assert exit_code == Verdict.CHAIN_OK_HOSTNAME_OK
    assert "verdict: CHAIN_OK_HOSTNAME_OK" in captured.out
    assert _PROBE_LEAF_COMMON_NAME in captured.out


def test_probe_host_leaf_does_not_cover_the_host_returns_verdict_11(
    probe_certificates: _ProbeCertificates,
    uncovered_server: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A verifiable chain with a foreign SAN is the one case for tls_verify_hostname: false."""
    result = probe_host(_PROBE_HOST, uncovered_server, probe_certificates.ca_bundle)
    exit_code = main(
        [
            "probe",
            "--host",
            _PROBE_HOST,
            "--port",
            str(uncovered_server),
            "--cafile",
            str(probe_certificates.ca_bundle),
        ]
    )

    captured = capsys.readouterr()
    assert result.verdict is Verdict.CHAIN_OK_HOSTNAME_MISMATCH
    assert exit_code == Verdict.CHAIN_OK_HOSTNAME_MISMATCH
    assert "verdict: CHAIN_OK_HOSTNAME_MISMATCH" in captured.out
    assert f"leaf SAN: {_UNCOVERED_HOST_NAME}" in captured.out


def test_probe_host_chain_not_issued_by_the_bundle_returns_verdict_12(
    matching_server: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unrelated bundle cannot verify the chain, whatever the host name says."""
    result = probe_host(_PROBE_HOST, matching_server, _TEST_CA_PEM_PATH)
    exit_code = main(
        [
            "probe",
            "--host",
            _PROBE_HOST,
            "--port",
            str(matching_server),
            "--cafile",
            str(_TEST_CA_PEM_PATH),
        ]
    )

    captured = capsys.readouterr()
    assert result.verdict is Verdict.CHAIN_FAIL
    assert exit_code == Verdict.CHAIN_FAIL
    assert "verdict: CHAIN_FAIL" in captured.out


def test_probe_host_nothing_listening_returns_verdict_13(
    probe_certificates: _ProbeCertificates, closed_port: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """No TCP session at all is a connection problem, not a certificate verdict."""
    result = probe_host(_PROBE_HOST, closed_port, probe_certificates.ca_bundle)
    exit_code = main(
        [
            "probe",
            "--host",
            _PROBE_HOST,
            "--port",
            str(closed_port),
            "--cafile",
            str(probe_certificates.ca_bundle),
        ]
    )

    captured = capsys.readouterr()
    assert result.verdict is Verdict.CONNECT_FAIL
    assert exit_code == Verdict.CONNECT_FAIL
    assert "verdict: CONNECT_FAIL" in captured.out
    assert "check DNS, VPN, port" in captured.out


def test_probe_host_cafile_without_certificate_returns_verdict_2(
    empty_bundle: Path, closed_port: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unusable bundle is named as such and never blamed on the network."""
    result = probe_host(_PROBE_HOST, closed_port, empty_bundle)
    exit_code = main(
        [
            "probe",
            "--host",
            _PROBE_HOST,
            "--port",
            str(closed_port),
            "--cafile",
            str(empty_bundle),
        ]
    )

    captured = capsys.readouterr()
    assert result.verdict is Verdict.BUNDLE_UNUSABLE
    assert exit_code == Verdict.BUNDLE_UNUSABLE
    assert "verdict: BUNDLE_UNUSABLE" in captured.out
    assert str(empty_bundle) in captured.out
    assert "check DNS, VPN, port" not in captured.out
