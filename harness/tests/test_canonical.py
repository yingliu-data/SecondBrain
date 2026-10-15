"""protocol.canonical — canonical JSON, hashing, derived ids."""
import hashlib
import json
import re

import pytest

from protocol.canonical import canonical_json, derive_id, hash_value, sha256_hex


# ---------------------------------------------------------------- canonical_json


def test_keys_sorted_recursively():
    assert canonical_json({"b": 1, "a": {"z": 1, "y": 2}}) == '{"a":{"y":2,"z":1},"b":1}'


def test_no_whitespace():
    text = canonical_json({"a": [1, 2, {"b": "c d"}]})
    assert text == '{"a":[1,2,{"b":"c d"}]}'
    assert re.sub(r'"[^"]*"', "", text).find(" ") == -1


def test_nfc_equivalence_in_values_and_keys():
    composed = "é"            # é
    decomposed = "é"         # e + combining acute
    assert composed != decomposed
    assert canonical_json(composed) == canonical_json(decomposed)
    assert canonical_json({composed: 1}) == canonical_json({decomposed: 1})
    assert hash_value([decomposed]) == hash_value([composed])


def test_ensure_ascii_false_keeps_utf8_text():
    text = canonical_json({"s": "é 日本"})
    assert text == '{"s":"é 日本"}'
    assert "\\u" not in text


def test_finite_floats_canonical_by_repr():
    assert canonical_json({"x": 1.5}) == '{"x":1.5}'
    assert canonical_json({"x": 1.0}) == '{"x":1.0}'      # int and float never collide
    assert canonical_json({"x": 1}) == '{"x":1}'
    assert canonical_json({"x": 0.1 + 0.2}) == '{"x":0.30000000000000004}'


def test_non_finite_floats_rejected():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="non-finite"):
            canonical_json({"x": bad})


def test_bools_none_and_nesting():
    assert canonical_json({"t": True, "f": False, "n": None, "i": 0, "neg": -3}) == \
        '{"f":false,"i":0,"n":null,"neg":-3,"t":true}'
    assert canonical_json([[], {}, [None, [True]]]) == '[[],{},[null,[true]]]'
    assert canonical_json((1, (2, 3))) == '[1,[2,3]]'


def test_bool_stays_bool_not_int():
    assert canonical_json(True) == "true"
    assert canonical_json(1) == "1"
    assert hash_value(True) != hash_value(1)


def test_non_string_keys_rejected():
    for key in (1, None, True, (1, 2)):
        with pytest.raises(ValueError, match="non-string key"):
            canonical_json({key: "x"})


def test_odd_types_rejected():
    for bad in (object(), {1, 2}, b"bytes", 1j):
        with pytest.raises(ValueError, match="not canonicalisable"):
            canonical_json(bad)


def test_canonical_json_round_trips_and_is_idempotent():
    value = {"b": [1, {"d": None, "c": "é"}], "a": True}
    text = canonical_json(value)
    assert json.loads(text) == value
    assert canonical_json(json.loads(text)) == text


def test_insertion_order_does_not_matter():
    assert hash_value({"a": 1, "b": 2}) == hash_value({"b": 2, "a": 1})


# ---------------------------------------------------------------- hashing


def test_sha256_known_vector():
    assert sha256_hex("abc") == \
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert sha256_hex("") == \
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_hex(b"abc") == sha256_hex("abc")


def test_sha256_of_text_is_utf8():
    assert sha256_hex("é") == hashlib.sha256("é".encode("utf-8")).hexdigest()
    assert sha256_hex("é") != hashlib.sha256("é".encode("latin-1")).hexdigest()


def test_hash_value_is_sha256_of_canonical_text():
    value = {"z": [1, "é"], "a": None}
    assert hash_value(value) == sha256_hex(canonical_json(value))
    assert len(hash_value(value)) == 64
    assert hash_value(value) == hash_value(value)


# ---------------------------------------------------------------- derive_id


def test_derive_id_deterministic():
    a = derive_id("u", "t", "s", 1, "turn")
    b = derive_id("u", "t", "s", 1, "turn")
    assert a == b


def test_derive_id_shape():
    i = derive_id("u", "t", "s", 7, "question")
    assert re.fullmatch(r"question_[0-9a-f]{24}", i)
    assert derive_id("u", "t", "s", 7, "op").startswith("op_")


def test_derive_id_distinct_per_input():
    base = derive_id("u", "t", "s", 1, "turn")
    assert derive_id("u2", "t", "s", 1, "turn") != base
    assert derive_id("u", "t2", "s", 1, "turn") != base
    assert derive_id("u", "t", "s2", 1, "turn") != base
    assert derive_id("u", "t", "s", 2, "turn") != base
    assert derive_id("u", "t", "s", 1, "op") != base
    assert derive_id("u", "t", "s", 1, "op")[3:] != base[5:]  # digests differ, not only the prefix


def test_derive_id_matches_documented_hash():
    digest = hash_value(["u", "t", "s", 3, "ticket"])
    assert derive_id("u", "t", "s", 3, "ticket") == "ticket_" + digest[:24]


def test_derive_id_seq_validation():
    for bad in (0, -1, True, 1.0, "1", None):
        with pytest.raises(ValueError, match="seq must be a positive int"):
            derive_id("u", "t", "s", bad, "turn")


def test_derive_id_kind_validation():
    for bad in ("", "tool_call", "_", None):
        with pytest.raises(ValueError, match="kind"):
            derive_id("u", "t", "s", 1, bad)
