def sort_records(records, sort, direction, fields, default):
    sort_key = str(sort or default).strip().lower()
    if sort_key not in fields:
        sort_key = default
    direction = "desc" if str(direction or "asc").strip().lower() == "desc" else "asc"
    getter = fields[sort_key]

    def value(record):
        raw = getter(record)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        if isinstance(raw, (list, tuple, set)):
            raw = " ".join(str(item or "") for item in raw)
        return str(raw or "").casefold()

    populated = []
    empty = []
    for record in records:
        raw = getter(record)
        (empty if raw is None or raw == "" else populated).append(record)
    populated.sort(
        key=lambda record: (
            value(record),
            str(record.get("path") or record.get("name") or record.get("id") or "").casefold(),
        ),
        reverse=direction == "desc",
    )
    return populated + empty, sort_key, direction
