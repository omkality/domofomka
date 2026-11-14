-- sqlfluff:dialect:sqlite
CREATE INDEX building_lookup ON buildings (street_key, house);
CREATE INDEX building_street ON buildings (city_key, street_key, house);
