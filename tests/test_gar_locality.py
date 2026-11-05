from tools.import_gar import locality


def test_regional_city_is_a_settlement_but_a_district_is_not():
    assert locality([("Люберцы", "г.", 2), ("Московская", "обл.", 1)], "50") == "Люберцы"
    assert locality([("Люберцы", "г.о.", 3), ("Московская", "обл.", 1)], "50") == "Московская"


def test_nested_settlement_and_federal_city_take_precedence():
    assert locality([("Томилино", "п.", 6), ("Люберцы", "г.", 2), ("Московская", "обл.", 1)], "50") == "Томилино"
    assert locality([("Москва", "г.", 1)], "77") == "Москва"
