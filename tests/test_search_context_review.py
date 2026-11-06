from tools.review_search_differences import context_score


def building(city, street, house):
    return {
        "city": city,
        "street": street,
        "house": house,
        "display": f"{city}, улица {street}, дом {house}",
        "lineage": city,
    }


def test_followup_target_does_not_make_other_plausible_menu_options_literal_errors():
    identities = {"a": building("Москва", "Перерва", "50"), "b": building("Тула", "Перерва", "50")}
    label = {"literal": {"house": "50"}, "observed_target": {"street": "Перерва", "house": "50", "city": "Москва"}}
    result = {"status": "ambiguous", "candidates": [{"building_id": "a"}, {"building_id": "b"}]}
    score = context_score(label, result, identities.get)
    assert not score["literal_conflict"]
    assert score["target_code_offered"]
    assert not score["other_automatic_target"]
    selected = context_score(label, {"status": "found", "building_id": "b"}, identities.get)
    assert selected["other_automatic_target"]
    assert not selected["literal_conflict"]


def test_explicit_city_is_required_for_every_menu_option_and_no_codes_is_separate():
    identities = {"a": building("Москва", "Перерва", "50"), "b": building("Тула", "Перерва", "50")}
    label = {"literal": {"city": "Москва", "house": "50"}, "observed_target": {"street": "Перерва", "house": "50"}}
    score = context_score(
        label, {"status": "ambiguous", "candidates": [{"building_id": "a"}, {"building_id": "b"}]}, identities.get
    )
    assert score["literal_conflict"]
    empty = context_score(label, {"status": "no_codes", "building_id": "a"}, identities.get)
    assert empty["target_offered"]
    assert not empty["target_code_offered"]
