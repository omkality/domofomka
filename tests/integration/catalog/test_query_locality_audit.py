import json
from pathlib import Path

from tools.history.audit_query_localities import LocalityMatcher, audit, extract_region, query_key


def facts():
    return [
        {"name": "Москва", "kind": "г", "region": "77", "level": 1},
        {"name": "Санкт-Петербург", "kind": "г", "region": "78", "level": 1},
        {"name": "Ростов", "kind": "г", "region": "76", "level": 5},
        {"name": "Ростов-на-Дону", "kind": "г", "region": "61", "level": 2},
        {"name": "Долгопрудный", "kind": "г", "region": "50", "level": 5},
        {"name": "Советск", "kind": "г", "region": "39", "level": 5},
        {"name": "Советск", "kind": "г", "region": "71", "level": 5},
        {"name": "Улица", "kind": "д", "region": "69", "level": 6},
        {"name": "Пронск", "kind": "рп", "region": "62", "level": 6},
    ]


def test_locality_mentions_are_whole_phrases_and_do_not_double_count_short_names():
    matcher = LocalityMatcher(facts())
    assert [m["name"] for m in matcher.find("Ростов-на-Дону, Пушкинская 5")] == ["Ростов-на-Дону"]
    assert matcher.find("Ростовская улица 5") == []
    assert matcher.find("московская 5") == []


def test_supported_city_aliases_are_evidence_not_a_city_prior():
    matcher = LocalityMatcher(facts())
    for query, name in [
        ("мск Перерва 1", "Москва"),
        ("питер Невский 1", "Санкт-Петербург"),
        ("СПб Невский 1", "Санкт-Петербург"),
    ]:
        assert matcher.find(query)[0]["name"] == name
        assert matcher.find(query)[0]["evidence"] == "city_alias"
    assert matcher.find("екб Тестовая 1") == []
    assert matcher.find("Тестовая 1") == []


def test_street_or_place_is_marked_uncertain_and_explicit_city_is_kept():
    matcher = LocalityMatcher(facts(), street_names={"долгопрудный"})
    assert matcher.find("Долгопрудный проезд 5")[0]["evidence"] == "street_or_locality"
    assert matcher.find("г. Долгопрудный, Лихачева 5")[0]["evidence"] == "explicit_locality"
    assert matcher.find("улица Улица 5")[0]["evidence"] == "street_or_locality"
    assert matcher.find("деревня Улица, Центральная 5")[0]["evidence"] == "explicit_locality"


def test_city_street_name_overlap_does_not_erase_a_separate_city_field():
    matcher = LocalityMatcher(facts(), street_names={"москва", "долгопрудный"})
    assert matcher.find("Москва, улица Перерва 5")[0]["evidence"] == "unmarked_city"
    assert matcher.find("Долгопрудный, улица Лихачева 5")[0]["evidence"] == "unmarked_city"
    assert matcher.find("Долгопрудный проезд 5")[0]["evidence"] == "street_or_locality"
    assert matcher.find("мск Перерва 5")[0]["evidence"] == "city_alias"


def test_city_without_house_multiple_cities_and_duplicate_names_keep_uncertainty():
    matcher = LocalityMatcher(facts())
    assert matcher.find("Пронск")[0]["name"] == "Пронск"
    assert len(matcher.find("Москва или Санкт-Петербург")) == 2
    assert matcher.find("Советск, Ленина 5")[0]["regions"] == ["39", "71"]


def test_numeric_settlement_names_do_not_turn_house_numbers_into_localities():
    matcher = LocalityMatcher(facts() + [{"name": "3", "kind": "рп", "region": "50", "level": 6}])
    assert matcher.find("Перерва 3") == []
    assert matcher.find("Перерва д. 3") == []
    assert matcher.find("поселок 3, Центральная 5")[0]["evidence"] == "explicit_locality"


def test_query_identity_keeps_different_house_forms_separate():
    queries = [
        "Москва Тестовая 2-4",
        "Москва Тестовая 24",
        "Москва Тестовая 2/4",
        "Москва Тестовая 2 4",
        "Москва Тестовая 2к4",
        "Москва Тестовая 2с4",
        "Москва Тестовая 2а",
    ]
    assert len({query_key(q) for q in queries}) == len(queries)
    assert query_key("  МОСКВА   Тестовая 2-4 ") == query_key(queries[0])


def test_official_snapshot_filters_inactive_and_administrative_objects(tmp_path):
    region = tmp_path / "61"
    region.mkdir()
    xml = region / "AS_ADDR_OBJ_20260101.XML"
    xml.write_text(
        """<ADDRESSOBJECTS>
      <OBJECT NAME="Ростов-на-Дону" TYPENAME="г" LEVEL="2" ISACTUAL="1" ISACTIVE="1"/>
      <OBJECT NAME="Пронск" TYPENAME="рп" LEVEL="6" ISACTUAL="1" ISACTIVE="1"/>
      <OBJECT NAME="Округ" TYPENAME="г.о." LEVEL="2" ISACTUAL="1" ISACTIVE="1"/>
      <OBJECT NAME="Ростовская" TYPENAME="ул" LEVEL="8" ISACTUAL="1" ISACTIVE="1"/>
      <OBJECT NAME="Старый" TYPENAME="г" LEVEL="5" ISACTUAL="0" ISACTIVE="1"/>
    </ADDRESSOBJECTS>""",
        encoding="utf8",
    )
    result = extract_region(region)
    assert {p["name"] for p in result["places"]} == {"Ростов-на-Дону", "Пронск"}
    assert result["source_sha256"]


def test_audit_counts_incoming_mentions_separately_from_bot_answer_and_deduplicates(tmp_path):
    source = tmp_path / "history"
    source.mkdir()
    (source / "conversations.json").write_text(json.dumps({"snapshot_at": 100, "peers": [1]}))
    messages = [
        {"id": 1, "date": 100, "out": 0, "text": "Ростов-на-Дону Ленина 5"},
        {"id": 2, "date": 101, "out": 1, "text": "Нет результатов"},
        {"id": 3, "date": 102, "out": 0, "text": "Пронск"},
        {"id": 4, "date": 103, "out": 0, "text": "улица Тестовая 5"},
        {"id": 4, "date": 103, "out": 0, "text": "улица Тестовая 5"},
        {"id": 5, "date": 104, "out": 1, "text": "Москва, улица Тестовая, дом 5"},
    ]
    (source / "dialog.json").write_text(json.dumps({"dialog": "private-dialog", "messages": messages}))
    output = tmp_path / "audit"
    summary = audit(source, output, LocalityMatcher(facts()))
    assert summary["counts"]["incoming_text"] == 3
    assert summary["counts"]["duplicate_events"] == 1
    assert summary["counts"]["without_locality_mention"] == 1
    by_name = {row["name"]: row for row in summary["places"]}
    assert by_name["Ростов-на-Дону"]["next_reply"]["no_results"] == 1
    assert by_name["Пронск"]["without_digits"] == 1
    assert "Москва" not in by_name
    public = json.dumps(summary, ensure_ascii=False)
    assert "private-dialog" not in public
    assert "Ленина" not in public
    assert "Тестовая" not in public
    assert (output / "incoming.jsonl").is_file()
    assert all(Path(row).name == row for row in summary["private_files"])
