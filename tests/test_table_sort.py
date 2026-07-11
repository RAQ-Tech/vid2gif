from app.table_sort import sort_records


def test_sort_records_orders_full_collection_before_pagination():
    records = [
        {"id": "a", "name": "Zulu", "size": 10},
        {"id": "b", "name": "Alpha", "size": 30},
        {"id": "c", "name": "Mike", "size": 20},
    ]

    ordered, key, direction = sort_records(
        records,
        "size",
        "desc",
        {"name": lambda item: item["name"], "size": lambda item: item["size"]},
        "name",
    )

    assert [item["id"] for item in ordered[:2]] == ["b", "c"]
    assert key == "size"
    assert direction == "desc"


def test_sort_records_rejects_unknown_fields_and_leaves_empty_values_last():
    records = [
        {"id": "empty", "name": ""},
        {"id": "z", "name": "Zulu"},
        {"id": "a", "name": "Alpha"},
    ]

    ordered, key, direction = sort_records(
        records,
        "not_allowed",
        "invalid",
        {"name": lambda item: item["name"]},
        "name",
    )

    assert [item["id"] for item in ordered] == ["a", "z", "empty"]
    assert key == "name"
    assert direction == "asc"
