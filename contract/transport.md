# Transport: how messages travel (observed with profile `ac-rad-fw6.0.032`)

Everything below was seen on 2026-09-16/17 with LibJciHitachi 1.7.2 + the per-device-availability branch.
"(code)" marks what is known only from reading `JciHitachi/aws_connection.py`; everything else was observed on
the wire. Nothing here explains *why* the cloud behaves as it does.

## 1. Authentication (HTTPS, AWS Cognito, region ap-northeast-1)

| step | call | observed |
|---|---|---|
| sign in | `InitiateAuth` `USER_PASSWORD_AUTH` on `cognito-idp` | returns `AccessToken` / `IdToken` / `RefreshToken` / `ExpiresIn`. Wrong credentials: `NotAuthorizedException`. Throttling / 5xx return other `__type` values, which are **not** credential errors (Cognito API reference). |
| identity | `GetUser` | `custom:cognito_identity_id` = `<identity>` (`ap-northeast-1:<uuid>`), `custom:host_identity_id` |
| MQTT credentials | `GetCredentialsForIdentity` on `cognito-identity` | temporary AWS credentials for the WebSocket MQTT connection (code) |

## 2. Device list (HTTPS, `iot-api.jci-hitachi-smarthome.com`)

`POST /GetAllDevice` with `authorization: Bearer <IdToken>` and `accesstoken: Bearer <AccessToken>`.
Response `status.code 0` and `results.Things[]`, one element per device: see
`profiles/ac-rad-fw6.0.032/get_all_device_thing.schema.json`. It carries **no model and no installed
firmware version**; `LatestFirmwareVersion` (`8.0.006:6`) is the version the cloud offers, not the one running
(`FirmwareVersion 6.0.032` comes from `registration/response`).

## 3. MQTT (AWS IoT, `a8kcu267h96in-ats.iot.ap-northeast-1.amazonaws.com`, WebSocket)

Thing name: `<identity>_<gateway id>` where the gateway id is a 14-digit decimal string (the same value is
the `client_token` of shadow requests, because tokens are limited to 64 bytes) (code + observed).

### 3.1 Request / response topics

The library subscribes once to `<identity>/+/+/response` (code). Requests are published per thing:

| request topic | payload | answer topic | answer body (profile) |
|---|---|---|---|
| `<identity>/<thing>/registration/request` | `{"Timestamp": <epoch float>}` | `<identity>/<thing>/registration/response` | `registration_response.schema.json` — capability masks + `FirmwareVersion`, `Model`, unit info |
| `<identity>/<thing>/status/request` | same | `<identity>/<thing>/status/response` | `status_response.schema.json` — current state |
| `<identity>/<thing>/control/request` | `{<field>: <value>, "TaskID": n, "Timestamp": ...}` (code) | `<identity>/<thing>/control/response` | **not captured** |
| (official app only) `.../status-secondary/request` | not seen | `.../status-secondary/response` | same key set as `status/response` (observed) |
| (official app only) `.../statistic/request` | not seen | `.../statistic/response` | not captured as JSON |

Because the subscription is a wildcard on the account, **every client of the account receives every
answer**, including answers to requests made by the official app or by another Home Assistant instance.
Observed: a Home Assistant host whose login had failed kept receiving the app's bursts (three devices ×
`statistic`, `registration`, `status` within 30 ms) for hours.

Answers arrive **per device**; in the same poll one device can answer and another not.

### 3.2 Non-JSON answers (observed, meaning unknown)

- A 6-byte payload `fc ff ff 1f 01 01` on `registration/response`, `status/response`,
  `status-secondary/response` and `statistic/response`. Per device, deterministic while the unit is in the
  state it is in, correlated with the unit **running** (details and timeline: `profile.json` →
  `non_json_answers`, and `tests/fixtures/observed_2026_09_16/README.md`).
- Once, a 761-byte payload on `registration/response` that is a **raw MQTT byte stream**: a complete
  MQTT PUBLISH packet (QoS 1, topic `.../status/response`, a valid status JSON inside) followed by the first
  54 bytes of the next PUBLISH. 761 bytes is exactly the length of that unit's normal `registration/response`
  JSON (`profile.json` → `nested_mqtt_publish_answer`).

Consequence for clients: a response event for a thing does not imply JSON; decode failures must be handled
per thing (this is what the per-device-availability branch does).

### 3.3 Named shadow `info`

`GetNamedShadow` (`$aws/things/<thing>/shadow/name/info/get`) with `clientToken = <gateway id>`; the accepted
document's `state.reported` is `shadow_info.schema.json`. The shadow answered JSON for a unit whose
`registration/response` was the 6-byte frame in the same poll.

The cloud also publishes shadow **updates on its own**, with **no client token**, carrying only
`{"online": false/true, "disconnectReason": "CLIENT_INITIATED_DISCONNECT" | ""}` when a client of the account
connects or disconnects. These are not replies to any request (observed every time a client disconnected).

### 3.4 Timing seen

- Answers normally arrive within about 1 s of the request.
- A unit that answers the 6-byte frame does so immediately (the frame is the answer, not a timeout).
- The library waits up to 10 s per request (code); with 1.7.2 an undecodable answer therefore cost the full
  10 s before the caller found out.

## 4. Encoding facts

- Payloads are JSON in UTF-8 **except** that `registration/response` carried one raw `0xFF` byte inside the
  `Model` string on every unit (`"RAD-\xffR"`), so decoding must use `errors="replace"`
  (library PR #33) and `Model` cannot be trusted.
- Whitespace: tab-indented, `"key":\t<value>` (as sent by the cloud; kept verbatim in the fixtures).
- Integers everywhere; `PowerConsumption` is tenths of a kWh (library divides by 10, code).
