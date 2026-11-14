CREATE INDEX building_lookup ON buildings (street_key, house);


CREATE INDEX building_street ON buildings (city_key, street_key, house);


CREATE UNIQUE INDEX codes_identity ON codes (building_id, entrance, kind, code);


CREATE INDEX aliases_building ON aliases (building_id);


CREATE INDEX alias_house ON alias_search (house);


CREATE INDEX street_words ON streets USING GIN (to_tsvector('simple', words)) WITH (fastupdate = OFF);


CREATE INDEX coded_street_words ON streets USING GIN (to_tsvector('simple', words)) WITH (fastupdate = OFF)
WHERE has_codes = 1;


CREATE INDEX alias_words ON alias_search USING GIN (to_tsvector('simple', words)) WITH (fastupdate = OFF);


INSERT INTO entrances
SELECT DISTINCT
  building_id,
  entrance
FROM
  codes;


INSERT INTO code_sources
SELECT DISTINCT
  kind
FROM
  codes;


ALTER TABLE codes ADD
FOREIGN KEY (building_id, entrance) REFERENCES entrances (building_id, number);


ALTER TABLE codes ADD
FOREIGN KEY (kind) REFERENCES code_sources (kind);


ALTER TABLE aliases ADD
FOREIGN KEY (building_id) REFERENCES buildings (id);
