import pytest

from orders import signature
from orders.signature import SignatureError
from tests.events import TEST_SECRET

BODY = b'{"id":"evt_test_1","type":"checkout.session.completed"}'
T = 1_800_000_000


def test_valid_signature_passes():
    signature.verify(BODY, signature.sign(BODY, TEST_SECRET, T), TEST_SECRET, now=T)


def test_known_vector():
    # Computed outside Python:
    #   printf %s '1700000000.{"a":1}' | openssl dgst -sha256 -hmac whsec_test
    assert (
        signature.compute(b'{"a":1}', "whsec_test", 1700000000)
        == "38877139021993b830af32feea6e18a8da83eb2f6e49ee50bd9e4cf4ca4d3789"
    )


def test_wrong_secret_fails():
    header = signature.sign(BODY, "whsec_test_someone_else", T)
    with pytest.raises(SignatureError, match="does not match"):
        signature.verify(BODY, header, TEST_SECRET, now=T)


def test_changed_body_fails():
    header = signature.sign(BODY, TEST_SECRET, T)
    with pytest.raises(SignatureError, match="does not match"):
        signature.verify(BODY.replace(b"evt_test_1", b"evt_test_2"), header, TEST_SECRET, now=T)


def test_reserialized_body_fails():
    # The same JSON with different whitespace is a different byte string.
    header = signature.sign(BODY, TEST_SECRET, T)
    with pytest.raises(SignatureError):
        signature.verify(BODY.replace(b":", b": "), header, TEST_SECRET, now=T)


def test_changed_timestamp_fails():
    digest = signature.compute(BODY, TEST_SECRET, T)
    with pytest.raises(SignatureError, match="does not match"):
        signature.verify(BODY, f"t={T + 1},v1={digest}", TEST_SECRET, now=T + 1)


@pytest.mark.parametrize("header", [None, "", "garbage", "t=abc,v1=00", f"t={T}", "v1=00"])
def test_missing_or_malformed_header_fails(header):
    with pytest.raises(SignatureError):
        signature.verify(BODY, header, TEST_SECRET, now=T)


def test_v0_only_is_not_accepted():
    digest = signature.compute(BODY, TEST_SECRET, T)
    with pytest.raises(SignatureError):
        signature.verify(BODY, f"t={T},v0={digest}", TEST_SECRET, now=T)


def test_replayed_old_request_fails():
    header = signature.sign(BODY, TEST_SECRET, T)
    signature.verify(BODY, header, TEST_SECRET, now=T + 300)
    with pytest.raises(SignatureError, match="tolerance"):
        signature.verify(BODY, header, TEST_SECRET, now=T + 301)


def test_timestamp_from_the_future_fails():
    header = signature.sign(BODY, TEST_SECRET, T + 301)
    with pytest.raises(SignatureError, match="tolerance"):
        signature.verify(BODY, header, TEST_SECRET, now=T)


def test_one_valid_signature_among_several_passes():
    # During a secret roll Stripe sends one v1 per active secret.
    good = signature.compute(BODY, TEST_SECRET, T)
    bad = signature.compute(BODY, "whsec_test_old", T)
    signature.verify(BODY, f"t={T},v1={bad},v1={good}", TEST_SECRET, now=T)


def test_empty_secret_never_verifies():
    header = signature.sign(BODY, "", T)
    with pytest.raises(SignatureError, match="no signing secret"):
        signature.verify(BODY, header, "", now=T)
