"""Схема индекса исторического лексического сравнения OpenSearch."""

MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "refresh_interval": "-1",
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "address": {"type": "custom", "tokenizer": "standard", "filter": ["lowercase", "russian_stemmer"]}
            },
            "filter": {"russian_stemmer": {"type": "stemmer", "language": "russian"}},
        },
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "id": {"type": "keyword"},
            "words": {"type": "text", "analyzer": "address"},
            "house": {"type": "keyword"},
        },
    },
}
