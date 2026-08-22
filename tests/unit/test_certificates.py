"""Unit tests for the forward proxy's PKI module (``proxy.certificates``).

Covers root CA generation, leaf certificate issuance with correct SAN and CA
signature, in-memory leaf certificate caching, reusing the CA from disk
across "restarts", key/directory file permissions, and proactive leaf
certificate reissuance as the expiry date approaches, plus thread safety of
that reissuance.
"""

from __future__ import annotations

import datetime
import stat
import threading
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding

from proxy import certificates
from proxy.certificates import CertificateAuthority, CertificateAuthorityError

_HOSTNAME = "api.anthropic.com"


def _verify_signed_by(certificate: x509.Certificate, issuer_certificate: x509.Certificate) -> None:
    """Verify that ``certificate`` is signed by ``issuer_certificate``'s private key.

    Args:
        certificate: the certificate under test (leaf).
        issuer_certificate: the certificate of the presumed issuer (CA).

    Raises:
        cryptography.exceptions.InvalidSignature: if the signature does not match.
    """
    issuer_certificate.public_key().verify(
        certificate.signature,
        certificate.tbs_certificate_bytes,
        padding.PKCS1v15(),
        certificate.signature_hash_algorithm,  # type: ignore[arg-type]
    )


def test_generated_ca_is_self_signed_with_ca_basic_constraint(tmp_path: Path) -> None:
    """The generated CA is a self-signed certificate with basicConstraints CA:true."""
    authority = CertificateAuthority(tmp_path / "ca")

    certificate = x509.load_pem_x509_certificate(authority.root_certificate_path().read_bytes())

    assert certificate.issuer == certificate.subject
    basic_constraints = certificate.extensions.get_extension_for_class(
        x509.BasicConstraints
    ).value
    assert basic_constraints.ca is True
    _verify_signed_by(certificate, certificate)


def test_generated_ca_common_name_is_human_recognizable(tmp_path: Path) -> None:
    """The root CA's CN is recognizable in a certificate list, not a generic string."""
    authority = CertificateAuthority(tmp_path / "ca")

    certificate = x509.load_pem_x509_certificate(authority.root_certificate_path().read_bytes())

    common_names = certificate.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    assert common_names[0].value == "open-harness-router local CA"


def test_leaf_certificate_contains_hostname_in_subject_alt_name(tmp_path: Path) -> None:
    """The leaf certificate's SAN contains the DNS name of the requested host."""
    authority = CertificateAuthority(tmp_path / "ca")

    leaf_certificate, _leaf_key = authority.leaf_certificate_for_host(_HOSTNAME)

    san = leaf_certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert san.get_values_for_type(x509.DNSName) == [_HOSTNAME]


def test_leaf_certificate_is_not_a_ca(tmp_path: Path) -> None:
    """The leaf certificate has basicConstraints CA:false."""
    authority = CertificateAuthority(tmp_path / "ca")

    leaf_certificate, _leaf_key = authority.leaf_certificate_for_host(_HOSTNAME)

    basic_constraints = leaf_certificate.extensions.get_extension_for_class(
        x509.BasicConstraints
    ).value
    assert basic_constraints.ca is False


def test_leaf_certificate_is_signed_by_the_root_ca(tmp_path: Path) -> None:
    """The leaf certificate's signature verifies against the CA's public key."""
    authority = CertificateAuthority(tmp_path / "ca")
    root_certificate = x509.load_pem_x509_certificate(
        authority.root_certificate_path().read_bytes()
    )

    leaf_certificate, _leaf_key = authority.leaf_certificate_for_host(_HOSTNAME)

    assert leaf_certificate.issuer == root_certificate.subject
    _verify_signed_by(leaf_certificate, root_certificate)


def test_repeated_request_for_same_host_returns_cached_certificate(tmp_path: Path) -> None:
    """A repeated leaf certificate request for the same host does not generate a new one."""
    authority = CertificateAuthority(tmp_path / "ca")

    first_certificate, first_key = authority.leaf_certificate_for_host(_HOSTNAME)
    second_certificate, second_key = authority.leaf_certificate_for_host(_HOSTNAME)

    assert first_certificate.serial_number == second_certificate.serial_number
    assert first_key is second_key


def test_different_hosts_receive_distinct_leaf_certificates(tmp_path: Path) -> None:
    """Different hosts receive different (uncached) leaf certificates."""
    authority = CertificateAuthority(tmp_path / "ca")

    first_certificate, _first_key = authority.leaf_certificate_for_host("first.example.com")
    second_certificate, _second_key = authority.leaf_certificate_for_host("second.example.com")

    assert first_certificate.serial_number != second_certificate.serial_number


def test_existing_ca_on_disk_is_reused_instead_of_regenerated(tmp_path: Path) -> None:
    """Re-initializing with the same directory reads the CA from disk, not a freshly created one."""
    ca_dir = tmp_path / "ca"
    first_authority = CertificateAuthority(ca_dir)
    first_certificate = x509.load_pem_x509_certificate(
        first_authority.root_certificate_path().read_bytes()
    )

    second_authority = CertificateAuthority(ca_dir)
    second_certificate = x509.load_pem_x509_certificate(
        second_authority.root_certificate_path().read_bytes()
    )

    assert first_certificate.serial_number == second_certificate.serial_number
    assert first_certificate.fingerprint(first_certificate.signature_hash_algorithm) == (  # type: ignore[arg-type]
        second_certificate.fingerprint(second_certificate.signature_hash_algorithm)  # type: ignore[arg-type]
    )


def test_corrupted_ca_key_file_on_disk_raises_certificate_authority_error(
    tmp_path: Path,
) -> None:
    """A corrupted CA key file on disk -- an explicit error, not silent degradation."""
    ca_dir = tmp_path / "ca"
    CertificateAuthority(ca_dir)
    (ca_dir / "rootCA-key.pem").write_bytes(b"not a valid pem key")

    with pytest.raises(CertificateAuthorityError):
        CertificateAuthority(ca_dir)


def test_ca_private_key_file_has_owner_only_permissions(tmp_path: Path) -> None:
    """The CA private key file is saved with 0600 permissions."""
    ca_dir = tmp_path / "ca"
    CertificateAuthority(ca_dir)

    key_mode = stat.S_IMODE((ca_dir / "rootCA-key.pem").stat().st_mode)

    assert key_mode == 0o600


def test_ca_directory_has_owner_only_permissions(tmp_path: Path) -> None:
    """The CA storage directory is saved with 0700 permissions."""
    ca_dir = tmp_path / "ca"
    CertificateAuthority(ca_dir)

    dir_mode = stat.S_IMODE(ca_dir.stat().st_mode)

    assert dir_mode == 0o700


def test_root_certificate_path_points_to_readable_pem_file(tmp_path: Path) -> None:
    """The path from ``root_certificate_path`` points to an existing PEM certificate file."""
    authority = CertificateAuthority(tmp_path / "ca")

    root_path = authority.root_certificate_path()

    assert root_path.exists()
    assert root_path.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")


def test_leaf_certificate_not_valid_before_is_backdated_for_clock_skew(
    tmp_path: Path,
) -> None:
    """``not_valid_before`` is backdated -- a client clock lag will not reject
    the certificate as not yet valid."""
    authority = CertificateAuthority(tmp_path / "ca")

    certificate, _leaf_key = authority.leaf_certificate_for_host(_HOSTNAME)

    now = datetime.datetime.now(datetime.UTC)
    assert certificate.not_valid_before_utc <= now - datetime.timedelta(hours=23)
    assert certificate.not_valid_before_utc > now - datetime.timedelta(days=2)


def test_leaf_certificate_well_within_validity_is_not_reissued(tmp_path: Path) -> None:
    """A certificate with more time left than the reissuance margin is not reissued."""
    authority = CertificateAuthority(tmp_path / "ca")

    first_certificate, first_key = authority.leaf_certificate_for_host(_HOSTNAME)
    second_certificate, second_key = authority.leaf_certificate_for_host(_HOSTNAME)

    assert second_certificate.serial_number == first_certificate.serial_number
    assert second_certificate.not_valid_after_utc == first_certificate.not_valid_after_utc
    assert second_key is first_key


def test_leaf_certificate_expiring_within_margin_is_reissued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A certificate with less time left than the reissuance margin is reissued early."""
    authority = CertificateAuthority(tmp_path / "ca")
    original_validity = certificates._LEAF_VALIDITY
    # Zero validity simulates a certificate that has reached the edge of the
    # reissuance margin (or already expired) -- without time-travel
    # libraries, this is the most direct way to get such a certificate in a
    # test.
    monkeypatch.setattr(certificates, "_LEAF_VALIDITY", datetime.timedelta(seconds=0))
    expiring_certificate, _expiring_key = authority.leaf_certificate_for_host(_HOSTNAME)
    monkeypatch.setattr(certificates, "_LEAF_VALIDITY", original_validity)

    renewed_certificate, _renewed_key = authority.leaf_certificate_for_host(_HOSTNAME)

    assert renewed_certificate.serial_number != expiring_certificate.serial_number
    assert renewed_certificate.not_valid_after_utc > expiring_certificate.not_valid_after_utc


def test_concurrent_requests_for_an_expiring_host_reissue_certificate_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent requests for one host's expiring certificate do not race."""
    authority = CertificateAuthority(tmp_path / "ca")
    monkeypatch.setattr(certificates, "_LEAF_VALIDITY", datetime.timedelta(seconds=0))
    authority.leaf_certificate_for_host(_HOSTNAME)
    monkeypatch.setattr(certificates, "_LEAF_VALIDITY", datetime.timedelta(days=30))

    thread_count = 16
    results: list[x509.Certificate] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(thread_count)

    def worker() -> None:
        barrier.wait()
        certificate, _key = authority.leaf_certificate_for_host(_HOSTNAME)
        with results_lock:
            results.append(certificate)

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == thread_count
    assert len({certificate.serial_number for certificate in results}) == 1
