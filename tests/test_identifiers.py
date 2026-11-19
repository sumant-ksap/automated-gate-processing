from gate.vision import identifiers as ids


def test_iso6346_known_valid_code():
    # example from the ISO 6346 standard
    assert ids.is_valid_iso6346("CSQU3054383")
    assert not ids.is_valid_iso6346("CSQU3054384")


def test_parse_trailer_formats_and_validates():
    assert ids.parse_trailer("CSQU 305438 3") == ("CSQU 305438 3", False)
    assert ids.parse_trailer("csqu-305438-3") == ("CSQU 305438 3", False)
    assert ids.parse_trailer("CSQU 305438 9") is None or ids.parse_trailer("CSQU 305438 9")[1]


def test_parse_trailer_repairs_ocr_confusions():
    # 'O' read instead of '0' in the serial, 'J' read instead of 'U' as category
    assert ids.parse_trailer("CSQU 3O5438 3") == ("CSQU 305438 3", True)
    value, corrected = ids.parse_trailer("CSQJ 305438 3")
    assert value == "CSQU 305438 3" and corrected


def test_parse_dot():
    assert ids.parse_dot("USDOT 2847193") == ("2847193", False)
    assert ids.parse_dot("US DOT # 2847I93") == ("2847193", True)
    assert ids.parse_dot("FREIGHT") is None


def test_parse_seal():
    assert ids.parse_seal("SEAL SL4821930") == ("SL4821930", False)
    assert ids.parse_seal("SEAL") is None


def test_plate_detection_and_variants():
    assert ids.looks_like_plate("7KXR392") == "7KXR392"
    assert ids.looks_like_plate("USDOT") is None
    assert ids.looks_like_plate("2847193") is None          # digits only - a DOT, not a plate
    assert "WN0H082" in ids.plate_variants("WNOH082")
    assert ids.levenshtein("ABC123", "ABD123") == 1
