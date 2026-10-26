from tesla_ro.vin import decode_vin


def test_decodes_model_3_fremont():
    info = decode_vin("5yj3e1ea9lf000316")
    assert info.valid
    assert info.model_name == "Model 3"
    assert info.year == 2020
    assert "Фрімонт" in info.plant
    assert info.warnings == []


def test_model_y_check_digit_x():
    info = decode_vin("7SAYGDEEXPF000001")
    assert info.valid and info.model_code == "MODEL_Y" and info.year == 2023
    assert info.warnings == []


def test_china_vin_skips_check_digit():
    info = decode_vin("LRW3E7EK0NC000001")
    assert info.valid and info.plant.startswith("Шанхай") and info.year == 2022
    assert info.warnings == []


def test_bad_check_digit_is_warning():
    info = decode_vin("5YJ3E1EA1LF000316")
    assert info.valid
    assert any("Контрольна цифра" in w for w in info.warnings)


def test_invalid_length_and_chars():
    assert not decode_vin("5YJ3E1").valid
    info = decode_vin("5YJ3E1EA9LF00031O")
    assert not info.valid and "O" in info.warnings[0]


def test_non_tesla():
    info = decode_vin("WVWZZZ1JZXW000001")
    assert not info.valid
