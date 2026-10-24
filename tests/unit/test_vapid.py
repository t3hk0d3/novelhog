"""Unit tests for the database-free parts of novelhog.vapid."""

from novelhog.vapid import VapidKeys


class TestVapidKeys:
    def test_repr__hides_the_private_key(self) -> None:
        keys = VapidKeys(public_key="PUBLIC", private_key="SECRET")
        assert "SECRET" not in repr(keys)
        assert "PUBLIC" in repr(keys)
