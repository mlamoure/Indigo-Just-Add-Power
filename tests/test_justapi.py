import json

from jap.justapi import HttpTimeout, JustApiClient
from tests.helpers import FakeHttp

DETAILS_B2 = json.dumps(
    {
        "data": {
            "mac": "C2:00:00:6C:32:DB",
            "model": "VBS-HDIP-707POE",
            "name": "Apple TV Tx",
            "version": "B2.4.1",
        }
    }
).encode()

IMAGEPULL_ENABLED = json.dumps(
    {"data": {"width": "320", "priority": "low", "frequency": "3"}}
).encode()
IMAGEPULL_DISABLED = json.dumps({"data": False}).encode()


def make_client(http, **kwargs):
    return JustApiClient("172.16.0.2", http=http, sleep=lambda s: None, **kwargs)


class TestGetDetails:
    def test_primary_path(self):
        http = FakeHttp([(r"GET .*cgi-bin/api/details/device", 200, DETAILS_B2)])
        details = make_client(http).get_details()
        assert details.mac == "c2:00:00:6c:32:db"
        assert details.model == "VBS-HDIP-707POE"
        assert details.device_name == "Apple TV Tx"
        assert details.firmware == "B2.4.1"
        assert details.raw["name"] == "Apple TV Tx"

    def test_fallback_path_probed_in_order(self):
        http = FakeHttp(
            [
                (r"GET http://172\.16\.0\.2/cgi-bin/api/details/device", 404, b"nope"),
                (r"GET http://172\.16\.0\.2/details/device", 200, DETAILS_B2),
            ]
        )
        details = make_client(http).get_details()
        assert details is not None
        assert [c[1] for c in http.calls] == [
            "http://172.16.0.2/cgi-bin/api/details/device",
            "http://172.16.0.2/details/device",
        ]

    def test_unreachable_returns_none(self):
        http = FakeHttp()  # no routes -> OSError
        assert make_client(http).get_details() is None

    def test_bad_mac_dropped_but_details_kept(self):
        payload = json.dumps({"data": {"mac": "garbage", "name": "X"}}).encode()
        http = FakeHttp([(r"details/device", 200, payload)])
        details = make_client(http).get_details()
        assert details.mac is None
        assert details.device_name == "X"

    def test_non_json_returns_none(self):
        http = FakeHttp([(r"details/device", 200, b"<html>login</html>")])
        assert make_client(http).get_details() is None


class TestIsOnline:
    def test_falls_back_to_imagepull_endpoint(self):
        http = FakeHttp([(r"settings/imagepull", 200, IMAGEPULL_DISABLED)])
        assert make_client(http).is_online() is True

    def test_offline(self):
        assert make_client(FakeHttp()).is_online() is False


class TestImagePull:
    def test_get_enabled_dict_shape(self):
        http = FakeHttp([(r"GET .*settings/imagepull", 200, IMAGEPULL_ENABLED)])
        status = make_client(http).get_image_pull()
        assert status.enabled is True
        assert status.width == 320
        assert status.priority == "low"
        assert status.frequency == 3

    def test_get_disabled_bool_shape(self):
        http = FakeHttp([(r"GET .*settings/imagepull", 200, IMAGEPULL_DISABLED)])
        status = make_client(http).get_image_pull()
        assert status.enabled is False

    def test_enable_flow_bodies_and_order(self):
        http = FakeHttp(
            [
                (r"POST .*settings/imagepull", 200, b"ok"),
                (r"POST .*command/device", 200, b"ok"),
            ]
        )
        sleeps = []
        client = JustApiClient("172.16.0.2", http=http, sleep=sleeps.append)
        assert client.enable_image_pull() is True
        posts = [(c[0], c[1], c[2]) for c in http.calls]
        assert posts[0] == (
            "POST",
            "http://172.16.0.2/cgi-bin/api/settings/imagepull",
            json.dumps({"width": "320", "priority": "low", "frequency": "3"}),
        )
        assert posts[1][2] == "save"
        assert posts[2][2] == "reboot"
        assert sleeps == [4, 4]

    def test_disable_body_is_null(self):
        http = FakeHttp(
            [
                (r"POST .*settings/imagepull", 200, b"ok"),
                (r"POST .*command/device", 200, b"ok"),
            ]
        )
        client = make_client(http)
        assert client.disable_image_pull() is True
        assert http.calls[0][2] == "null"


class TestReboot:
    def test_timeout_is_success(self):
        http = FakeHttp([(r"POST .*command/device", 0, HttpTimeout("dropped"))])
        assert make_client(http).reboot() is True

    def test_connection_error_is_failure(self):
        http = FakeHttp([(r"POST .*command/device", 0, OSError("refused"))])
        assert make_client(http).reboot() is False

    def test_reboot_uses_short_timeout(self):
        http = FakeHttp([(r"POST .*command/device", 200, b"ok")])
        make_client(http).reboot()
        assert http.calls[0][3] == 1.0


class TestSnapshots:
    def test_url_order_portless_first(self):
        client = make_client(FakeHttp())
        assert client.image_pull_urls() == [
            "http://172.16.0.2/pull.bmp",
            "http://172.16.0.2:8080/pull.bmp",
        ]

    def test_fetch_image_falls_back_to_8080(self):
        http = FakeHttp(
            [
                (r"GET http://172\.16\.0\.2/pull\.bmp", 404, b""),
                (r"GET http://172\.16\.0\.2:8080/pull\.bmp", 200, b"BMPDATA"),
            ]
        )
        url, data = make_client(http).fetch_image()
        assert url == "http://172.16.0.2:8080/pull.bmp"
        assert data == b"BMPDATA"

    def test_fetch_image_none_when_unreachable(self):
        assert make_client(FakeHttp()).fetch_image() is None


class TestJpsw:
    def test_set_channel_posts_channel_number(self):
        http = FakeHttp([(r"POST .*command/channel", 200, b"ok")])
        assert make_client(http).set_channel(3) is True
        assert http.calls[0][1] == "http://172.16.0.2/cgi-bin/api/command/channel"
        assert http.calls[0][2] == "3"

    def test_never_uses_command_cli(self):
        # Guard: the /command/cli endpoint has a JSON-breaking bug on B1.x.
        http = FakeHttp(
            [
                (r"POST .*command/channel", 200, b"ok"),
                (r"GET .*details/channel", 200, b'{"data": 2}'),
            ]
        )
        client = make_client(http)
        client.set_channel(1)
        client.get_channel()
        assert all("/command/cli" not in call[1] for call in http.calls)

    def test_get_channel(self):
        http = FakeHttp([(r"GET .*details/channel", 200, b'{"data": "5"}')])
        assert make_client(http).get_channel() == 5
