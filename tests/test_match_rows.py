from types import SimpleNamespace

from ops.api.routes import matched_track_rows


def test_rows_follow_saved_identity_not_order_or_title():
    a = SimpleNamespace(key="a", title="Original")
    b = SimpleNamespace(key="b", title="Second")
    alternate = SimpleNamespace(key="a", title="Different upload name")
    second = SimpleNamespace(key="b", title="Second")
    assert matched_track_rows([{"tracks": [a, b]}, {"tracks": [second, alternate]}]) == [
        [a, alternate],
        [b, second],
    ]


def test_unmatched_and_duplicate_occurrences_are_retained():
    a = SimpleNamespace(key="a")
    b = SimpleNamespace(key="b")
    assert matched_track_rows([{"tracks": [a, a]}, {"tracks": [a, b]}]) == [
        [a, a],
        [a, None],
        [None, b],
    ]
    assert matched_track_rows([]) == []
