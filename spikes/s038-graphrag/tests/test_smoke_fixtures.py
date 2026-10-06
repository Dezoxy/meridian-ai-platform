from dbsupport import DatabaseHandle


def test_a_fresh_database_is_reachable_from_the_spike(
    fresh_database: DatabaseHandle,
) -> None:
    assert fresh_database.name.startswith("meridian_test_")
