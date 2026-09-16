"""Per-device failure tolerance: one device answering garbage must not take the others down."""

import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from JciHitachi.api import AWSThing, JciHitachiAWSAPI
from JciHitachi.aws_connection import (
    AWSTokens,
    JciHitachiAuthError,
    JciHitachiAWSMqttConnection,
    JciHitachiDeviceError,
)
from JciHitachi.model import JciHitachiAWSStatus, JciHitachiAWSStatusSupport

IDENTITY = "ap-northeast-1:8916b515-8394-4ccd-95b8-4f553c13dafa"
GW_A = "10416149025290813292"
GW_B = "10416149025290813293"
BINARY_FRAME = (
    b"\xfc\xff\xff\x1f\x01\x01"  # observed from three RAD-series ACs, 2026-08/09
)


def _thing(name, gw):
    return AWSThing(
        {"DeviceType": "1", "ThingName": f"{IDENTITY}_{gw}", "CustomDeviceName": name}
    )


@pytest.fixture()
def mqtt():
    return JciHitachiAWSMqttConnection(lambda: None)


@pytest.fixture()
def api():
    api = JciHitachiAWSAPI("", "", None)
    api._things = {
        "Device A": _thing("Device A", GW_A),
        "Device B": _thing("Device B", GW_B),
    }
    api._aws_identity = MagicMock(host_identity_id=IDENTITY)
    # valid-looking tokens so _check_before_publish() never tries to reauthenticate for real
    api._aws_tokens = AWSTokens("", "", "", expiration=time.time() + 3600)
    return api


class TestOnPublishUndecodable:
    def test_binary_registration_response_is_recorded_and_releases_waiter(
        self, mqtt, caplog
    ):
        thing = f"{IDENTITY}_{GW_A}"
        topic = f"{IDENTITY}/{thing}/registration/response"
        mqtt._mqtt_events.device_support_event[thing] = threading.Event()
        mqtt._mqtt_events.mqtt_error_event.clear()

        with caplog.at_level(logging.DEBUG):
            mqtt._on_publish(topic, BINARY_FRAME, None, None, None)

        assert mqtt._mqtt_events.device_undecodable[thing] == {
            "registration": (topic, BINARY_FRAME)
        }
        assert mqtt._mqtt_events.device_support_event[thing].is_set(), (
            "waiter must not burn the timeout"
        )
        assert thing not in mqtt._mqtt_events.device_support
        assert not mqtt._mqtt_events.mqtt_error_event.is_set(), (
            "a per-device frame must not trigger reauth"
        )
        assert "fcffff1f0101" in caplog.text

    def test_binary_frame_on_unrequested_topic_does_not_raise(self, mqtt):
        thing = f"{IDENTITY}_{GW_A}"
        # `statistic` and `status-secondary` are only requested by the official app
        mqtt._on_publish(
            f"{IDENTITY}/{thing}/statistic/response", BINARY_FRAME, None, None, None
        )
        mqtt._on_publish(
            f"{IDENTITY}/{thing}/status-secondary/response",
            BINARY_FRAME,
            None,
            None,
            None,
        )
        assert set(mqtt._mqtt_events.device_undecodable[thing]) == {
            "statistic",
            "status-secondary",
        }

    def test_undecodable_without_thing_keeps_global_error(self, mqtt):
        mqtt._mqtt_events.mqtt_error_event.clear()
        mqtt._on_publish("", b"", None, None, None)
        assert mqtt._mqtt_events.mqtt_error_event.is_set()

    def test_response_before_publish_created_event_does_not_raise(self, mqtt):
        thing = f"{IDENTITY}_{GW_A}"
        assert thing not in mqtt._mqtt_events.device_status_event
        # previously KeyError inside the awscrt callback thread
        mqtt._on_publish(
            f"{IDENTITY}/{thing}/status/response",
            b'{"DeviceType": 1}',
            None,
            None,
            None,
        )
        assert isinstance(mqtt._mqtt_events.device_status[thing], JciHitachiAWSStatus)

    def test_good_response_clears_earlier_undecodable(self, mqtt):
        thing = f"{IDENTITY}_{GW_A}"
        mqtt._on_publish(
            f"{IDENTITY}/{thing}/status/response", BINARY_FRAME, None, None, None
        )
        mqtt._on_publish(
            f"{IDENTITY}/{thing}/status/response",
            b'{"DeviceType": 1}',
            None,
            None,
            None,
        )
        assert "status" not in mqtt._mqtt_events.device_undecodable.get(thing, {})


class TestShadowWithoutClientToken:
    def test_cloud_initiated_update_is_ignored_quietly(self, mqtt, caplog):
        response = MagicMock()
        response.client_token = None
        response.state.reported = {
            "online": False,
            "disconnectReason": "CLIENT_INITIATED_DISCONNECT",
        }
        with caplog.at_level(logging.DEBUG):
            mqtt._on_update_named_shadow_accepted(response)
            mqtt._on_get_named_shadow_accepted(response)
        assert "unknown shadow response" not in caplog.text
        assert mqtt._mqtt_events.device_control == {}


class TestRefreshStatusPerDevice:
    def _mock_mqtt(self, api, execute_result):
        mock = MagicMock()
        mock.execute.return_value = execute_result
        mock.mqtt_events.mqtt_error_event.is_set.return_value = False
        mock.mqtt_events.device_status = {}
        mock.mqtt_events.device_support = {}
        mock.mqtt_events.device_shadow = {}
        mock.mqtt_events.device_undecodable = {}
        api._mqtt = mock
        return mock

    def test_one_undecodable_device_does_not_abort_the_other(self, api):
        a = api.things["Device A"].thing_name
        b = api.things["Device B"].thing_name
        mock = self._mock_mqtt(api, [[a, b], [a, b], [a, b], []])
        support = JciHitachiAWSStatusSupport(
            {"DeviceType": 1, "TemperatureSetting": 4128}
        )
        status = JciHitachiAWSStatus({"DeviceType": 1, "TemperatureSetting": 26})
        mock.mqtt_events.device_support = {b: support}
        mock.mqtt_events.device_shadow = {
            a: {"CleanNotification": True},
            b: {"CleanNotification": False},
        }
        mock.mqtt_events.device_status = {b: status}
        mock.mqtt_events.device_undecodable = {
            a: {"registration": (f"{IDENTITY}/{a}/registration/response", BINARY_FRAME)}
        }

        api.refresh_status(refresh_support_code=True, refresh_shadow=True)  # no raise

        thing_a, thing_b = api.things["Device A"], api.things["Device B"]
        assert thing_a.available is False
        assert "not JSON (hex fcffff1f0101)" in thing_a.attention_reason
        assert "registration/response" in thing_a.attention_reason
        assert thing_a.support_code is None and thing_a.status_code is None
        # the shadow channel did answer, so it is kept even though the device failed
        assert thing_a.shadow == {"CleanNotification": True}
        assert thing_b.available is True and thing_b.attention_reason is None
        assert thing_b.support_code is support and thing_b.status_code is status

        statuses = api.get_status()
        assert list(statuses) == ["Device B"], "never-refreshed devices are skipped"
        assert statuses["Device B"].max_temp == 32

    def test_all_devices_failing_raises_device_error_listing_each(self, api):
        self._mock_mqtt(api, [[], [], [BaseException, BaseException], []])
        with pytest.raises(JciHitachiDeviceError) as exc:
            api.refresh_status()
        assert isinstance(exc.value, RuntimeError)
        assert "Device A" in str(exc.value) and "Device B" in str(exc.value)
        assert all(not t.available for t in api.things.values())

    def test_device_recovers_on_next_refresh(self, api):
        a = api.things["Device A"].thing_name
        api._things = {"Device A": api.things["Device A"]}
        mock = self._mock_mqtt(api, [[], [], [BaseException], []])
        with pytest.raises(JciHitachiDeviceError):
            api.refresh_status()
        assert api.things["Device A"].available is False

        mock.execute.return_value = [[], [], [a], []]
        mock.mqtt_events.device_status = {a: JciHitachiAWSStatus({"DeviceType": 1})}
        api.refresh_status()
        assert api.things["Device A"].available is True
        assert api.things["Device A"].attention_reason is None


class TestThingWithoutSupportCode:
    def test_properties_are_none_safe(self):
        thing = _thing("Device A", GW_A)
        assert thing.brand is None
        assert thing.model is None
        assert thing.firmware_version is None
        assert thing.firmware_code is None

    def test_corrupted_model_string_is_reported_as_unknown(self):
        # observed 2026-09-16 in a registration/response: "Model": "RAD-\xffR"
        thing = _thing("Device A", GW_A)
        thing.support_code = JciHitachiAWSStatusSupport(
            {"DeviceType": 1, "Model": "RAD-�\x06\x01R", "FirmwareVersion": "6.0.032"}
        )
        assert thing.model is None
        assert thing.firmware_version == "6.0.032"
        thing.support_code = JciHitachiAWSStatusSupport(
            {"DeviceType": 1, "Model": "RAD-90NF"}
        )
        assert thing.model == "RAD-90NF"


class TestNoStaleAnswers:
    def test_publish_forgets_the_previous_answer(self, mqtt):
        """A request must not be satisfied by the answer to the previous one (good-then-frame)."""
        thing = f"{IDENTITY}_{GW_A}"
        mqtt._mqtt_events.device_status[thing] = JciHitachiAWSStatus({"DeviceType": 1})
        mqtt._mqtt_events.device_support[thing] = JciHitachiAWSStatusSupport(
            {"DeviceType": 1}
        )
        mqtt._mqtt_events.device_control[thing] = {"Switch": 1}
        mqtt._mqtt_events.device_shadow[thing] = {"online": True}
        with patch.object(mqtt, "_mqttc"), patch.object(mqtt, "_shadow_mqttc"):
            mqtt.publish(IDENTITY, thing, "status")
            mqtt.publish(IDENTITY, thing, "support")
            mqtt.publish(IDENTITY, thing, "control", payload={})
            mqtt.publish_shadow(thing, "get", shadow_name="info")
        for pool in (
            mqtt._execution_pools.status_execution_pool,
            mqtt._execution_pools.support_execution_pool,
            mqtt._execution_pools.control_execution_pool,
            mqtt._execution_pools.shadow_execution_pool,
        ):
            for coro in pool:
                coro.close()
            pool.clear()
        assert thing not in mqtt._mqtt_events.device_status
        assert thing not in mqtt._mqtt_events.device_support
        assert thing not in mqtt._mqtt_events.device_control
        assert thing not in mqtt._mqtt_events.device_shadow

    def test_set_status_returns_false_on_undecodable_control_answer(self, api, caplog):
        a = api.things["Device A"].thing_name
        api.things["Device A"].status_code = JciHitachiAWSStatus(
            {"DeviceType": 1, "Switch": 0}
        )
        mock = MagicMock()
        mock.execute.return_value = [None, None, None, [a]]
        mock.mqtt_events.mqtt_error_event.is_set.return_value = False
        mock.mqtt_events.device_control = {}
        mock.mqtt_events.device_undecodable = {
            a: {"control": (f"{IDENTITY}/{a}/control/response", BINARY_FRAME)}
        }
        api._mqtt = mock
        with caplog.at_level(logging.WARNING):
            assert api.set_status("Switch", "Device A", status_str_value="on") is False
        assert "fcffff1f0101" in caplog.text


class TestCognitoErrorClassification:
    def test_only_credential_errors_are_auth_errors(self):
        from JciHitachi.aws_connection import cognito_error

        assert isinstance(
            cognito_error(
                "NotAuthorizedException Incorrect username or password.", "x"
            ),
            JciHitachiAuthError,
        )
        assert isinstance(
            cognito_error("UserNotFoundException User does not exist.", "x"),
            JciHitachiAuthError,
        )
        transient = cognito_error("TooManyRequestsException Rate exceeded", "x")
        assert isinstance(transient, RuntimeError)
        assert not isinstance(transient, JciHitachiAuthError)
        assert not isinstance(
            cognito_error("InternalErrorException", "x"), JciHitachiAuthError
        )


THINGS_JSON = {
    "results": {
        "Things": [
            {
                "DeviceType": "1",
                "ThingName": f"{IDENTITY}_{GW_A}",
                "CustomDeviceName": "Device A",
            }
        ]
    }
}


def _login_patches(refresh_side_effect):
    identity = MagicMock(identity_id=IDENTITY, host_identity_id=IDENTITY)
    return [
        patch("JciHitachi.aws_connection.GetUser.__init__", return_value=None),
        patch(
            "JciHitachi.aws_connection.GetUser.aws_tokens",
            new_callable=lambda: property(lambda self: MagicMock()),
        ),
        patch(
            "JciHitachi.aws_connection.GetUser.get_data", return_value=("OK", identity)
        ),
        patch(
            "JciHitachi.aws_connection.GetAllDevice.get_data",
            return_value=("OK", THINGS_JSON),
        ),
        patch("JciHitachi.aws_connection.JciHitachiAWSMqttConnection.configure"),
        patch(
            "JciHitachi.aws_connection.JciHitachiAWSMqttConnection.connect",
            return_value=True,
        ),
        patch(
            "JciHitachi.api.JciHitachiAWSAPI.refresh_status",
            side_effect=refresh_side_effect,
        ),
    ]


class TestLoginCleanup:
    def test_all_devices_failing_still_logs_in(self, api, caplog):
        patches = _login_patches(JciHitachiDeviceError("Device A timed out"))
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch(
                "JciHitachi.aws_connection.JciHitachiAWSMqttConnection.disconnect"
            ) as disconnect,
            caplog.at_level(logging.WARNING),
        ):
            api.login()
        disconnect.assert_not_called()
        assert "no device is available yet" in caplog.text

    def test_unexpected_failure_disconnects_mqtt(self, api):
        patches = _login_patches(RuntimeError("boom"))
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch(
                "JciHitachi.aws_connection.JciHitachiAWSMqttConnection.disconnect"
            ) as disconnect,
        ):
            with pytest.raises(RuntimeError, match="boom"):
                api.login()
        disconnect.assert_called_once()

    def test_identity_failure_is_an_auth_error(self, api):
        with (
            patch("JciHitachi.aws_connection.GetUser.__init__", return_value=None),
            patch(
                "JciHitachi.aws_connection.GetUser.aws_tokens",
                new_callable=lambda: property(lambda self: MagicMock()),
            ),
            patch(
                "JciHitachi.aws_connection.GetUser.get_data",
                return_value=("NotAuthorizedException", None),
            ),
        ):
            with pytest.raises(JciHitachiAuthError):
                api.login()
