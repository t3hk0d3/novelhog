"""Unit tests for novelhog.config."""

import os
import socket
from pathlib import Path

import pytest

from novelhog import config


class TestLoad__WithNoVariables:
    def test_load__returns_spec_defaults(self) -> None:
        s = config.load({})
        assert s.data_dir == Path("/data")
        assert s.public_url is None
        assert s.vapid_subject is None
        assert s.flaresolverr_url == "http://flaresolverr:8191"
        assert s.scraper_dir == Path("/var/lib/novelhog/scraper")
        assert s.static_backend == "local"
        assert s.static_base_url == "/static"
        assert s.refresh_min_interval == 21600
        assert s.refresh_max_interval == 2592000
        assert s.claim_ttl == 300
        assert s.idle_sleep == 30
        assert s.request_delay == 1.0
        assert s.max_toc_pages == 1000
        assert s.max_image_bytes == 2097152
        assert s.max_image_pixels == 40000000
        assert s.image_max_dimension == 1600
        assert s.allow_private_fetch is False
        assert s.http_port == 8080

    def test_load__derives_database_url_and_static_dir_from_data_dir(self) -> None:
        s = config.load({})
        assert s.database_url == "sqlite:////data/db/app.db"
        assert s.static_dir == Path("/data/static")

    def test_load__worker_id_is_hostname_and_pid(self) -> None:
        assert config.load({}).worker_id == f"{socket.gethostname()}:{os.getpid()}"

    def test_load__webpush_disabled_and_allowed_hosts_empty(self) -> None:
        s = config.load({})
        assert s.webpush_enabled is False
        assert s.allowed_hosts == ()


class TestLoad__WithDataDirOnly:
    def test_load__database_url_follows_data_dir(self) -> None:
        s = config.load({"NOVELHOG_DATA_DIR": "/srv"})
        assert s.database_url == "sqlite:////srv/db/app.db"

    def test_load__static_dir_follows_data_dir(self) -> None:
        s = config.load({"NOVELHOG_DATA_DIR": "/srv"})
        assert s.static_dir == Path("/srv/static")


class TestLoad__WithExplicitDatabaseUrl:
    def test_load__uses_it_verbatim(self) -> None:
        url = "postgresql+psycopg://u:p@db/novelhog"
        assert config.load({"NOVELHOG_DATABASE_URL": url}).database_url == url


class TestLoad__WithPushVariables:
    def test_load__webpush_enabled_is_true(self) -> None:
        s = config.load(
            {
                "NOVELHOG_PUBLIC_URL": "https://n.example",
                "NOVELHOG_VAPID_SUBJECT": "mailto:me@example.com",
            }
        )
        assert s.webpush_enabled is True

    def test_load__public_url_alone_leaves_webpush_disabled(self) -> None:
        assert config.load({"NOVELHOG_PUBLIC_URL": "https://n.example"}).webpush_enabled is False


class TestLoad__WithAllowedHosts:
    def test_load__splits_and_strips_names(self) -> None:
        s = config.load({"NOVELHOG_ALLOWED_HOSTS": "a.lan, b.lan"})
        assert s.allowed_hosts == ("a.lan", "b.lan")


class TestLoad__WithAllowPrivateFetch:
    @pytest.mark.parametrize(
        ("raw", "expected"), [("true", True), ("1", True), ("false", False), ("0", False)]
    )
    def test_load__parses_the_boolean(self, raw: str, expected: bool) -> None:
        assert config.load({"NOVELHOG_ALLOW_PRIVATE_FETCH": raw}).allow_private_fetch is expected

    def test_load__other_value_raises_naming_the_variable(self) -> None:
        with pytest.raises(ValueError, match="NOVELHOG_ALLOW_PRIVATE_FETCH"):
            config.load({"NOVELHOG_ALLOW_PRIVATE_FETCH": "maybe"})


class TestLoad__WithNonNumericValue:
    def test_load__integer_variable_raises_naming_the_variable(self) -> None:
        with pytest.raises(ValueError, match="NOVELHOG_CLAIM_TTL"):
            config.load({"NOVELHOG_CLAIM_TTL": "ten"})

    def test_load__float_variable_raises_naming_the_variable(self) -> None:
        with pytest.raises(ValueError, match="NOVELHOG_REQUEST_DELAY"):
            config.load({"NOVELHOG_REQUEST_DELAY": "soon"})


class TestLoad__WithNumericOverrides:
    def test_load__parses_integer_and_float_values(self) -> None:
        s = config.load({"NOVELHOG_CLAIM_TTL": "25", "NOVELHOG_REQUEST_DELAY": "0.5"})
        assert s.claim_ttl == 25
        assert s.request_delay == 0.5


class TestLoad__WithUnknownStaticBackend:
    def test_load__raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="NOVELHOG_STATIC_BACKEND"):
            config.load({"NOVELHOG_STATIC_BACKEND": "s3"})


class TestLoad__WithEmptyValues:
    def test_load__empty_data_dir_uses_the_default(self) -> None:
        s = config.load({"NOVELHOG_DATA_DIR": ""})
        assert s.data_dir == Path("/data")
        assert s.database_url == "sqlite:////data/db/app.db"

    def test_load__empty_numeric_value_uses_the_default(self) -> None:
        assert config.load({"NOVELHOG_CLAIM_TTL": ""}).claim_ttl == 300


class TestLoad__WithMixedCaseAllowedHosts:
    def test_load__lowercases_names_and_drops_empty_entries(self) -> None:
        s = config.load({"NOVELHOG_ALLOWED_HOSTS": "A.Lan,, B.LAN ,"})
        assert s.allowed_hosts == ("a.lan", "b.lan")


class TestLoad__WithTheRemovedBatchSize:
    @pytest.mark.parametrize("value", ["10", "ten"])
    def test_load__ignores_it_and_names_it_in_ignored(self, value: str) -> None:
        s = config.load({"NOVELHOG_BATCH_SIZE": value})

        assert not hasattr(s, "batch_size")
        assert s.ignored == ("NOVELHOG_BATCH_SIZE",)

    def test_load__ignores_nothing_when_it_is_unset_or_empty(self) -> None:
        assert config.load({}).ignored == ()
        assert config.load({"NOVELHOG_BATCH_SIZE": ""}).ignored == ()
