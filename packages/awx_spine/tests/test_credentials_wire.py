from decimal import Decimal

import pytest
from awx_spine.credentials import (
    AirwallexCredentials,
    CredentialsUnavailable,
    EnvFileCredentials,
    EnvVarCredentials,
)
from awx_spine.redact import Redactor, safe_headers
from awx_spine.wire import decode_json, encode_json
from hypothesis import given
from hypothesis import strategies as st

SECRET = "sk_live_should_never_be_seen_0123456789"


def test_env_file_parses_and_never_echoes_values(tmp_path):
    f = tmp_path / "credentials.env"
    f.write_text(f"# comment\nexport AWX_CLIENT_ID='cid-123'\nAWX_API_KEY=\"{SECRET}\"\n")
    got = EnvFileCredentials(f).get()
    assert got.client_id == "cid-123"
    assert got.api_key == SECRET
    assert SECRET not in repr(got)
    assert SECRET not in str(got)
    assert SECRET not in repr(EnvFileCredentials(f))


def test_env_file_errors_name_the_path_not_the_value(tmp_path):
    with pytest.raises(CredentialsUnavailable, match="cannot read"):
        EnvFileCredentials(tmp_path / "missing.env").get()
    f = tmp_path / "half.env"
    f.write_text(f"AWX_API_KEY={SECRET}\n")
    with pytest.raises(CredentialsUnavailable) as info:
        EnvFileCredentials(f).get()
    assert SECRET not in str(info.value)


def test_env_var_credentials():
    got = EnvVarCredentials({"AWX_CLIENT_ID": "c", "AWX_API_KEY": SECRET}).get()
    assert got.api_key == SECRET
    with pytest.raises(CredentialsUnavailable):
        EnvVarCredentials({"AWX_CLIENT_ID": "c"}).get()
    with pytest.raises(CredentialsUnavailable):
        AirwallexCredentials("", "x")


def test_redactor_masks_known_secrets_and_auth_patterns():
    r = Redactor([SECRET])
    text = f"key={SECRET} Authorization: Bearer abcdefghijklmnop x-client-id: cid"
    out = r.text(text)
    assert SECRET not in out
    assert "abcdefghijklmnop" not in out
    assert "cid" not in out
    assert r.value({"Authorization": "x", "nested": [f"v {SECRET}"]}) == {
        "Authorization": "***",
        "nested": ["v ***"],
    }
    assert safe_headers({"Authorization": "Bearer x", "Content-Type": "a"}) == {
        "Authorization": "***",
        "Content-Type": "a",
    }


def test_wire_encodes_decimals_exactly():
    body = {"a": Decimal("10.00"), "b": Decimal("0.1"), "c": [Decimal("-0.00"), 3, True, None]}
    assert encode_json(body) == b'{"a":10.00,"b":0.1,"c":[0.00,3,true,null]}'
    with pytest.raises(TypeError, match="float"):
        encode_json({"x": 0.1})
    with pytest.raises(ValueError, match="non-finite"):
        encode_json({"x": Decimal("NaN")})
    from awx_common import Money

    with pytest.raises(TypeError, match="Money"):
        encode_json({"x": Money.of("1", "USD")})


@given(
    st.decimals(
        allow_nan=False, allow_infinity=False, places=6, min_value=-(10**12), max_value=10**12
    )
)
def test_wire_round_trip_is_exact(value):
    got = decode_json(encode_json({"v": value}))["v"]
    assert Decimal(got) == value
    assert not isinstance(got, float)
