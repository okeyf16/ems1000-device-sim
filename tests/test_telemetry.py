import json
import random
import re

import pytest

from sim.sensors import SensorModel, SensorReading
from sim.telemetry import SCHEMA_V1, PayloadError, build_payload, new_traceparent, validate

FIRMWARE_FIELD_ORDER = ["sv", "id", "ts", "boot", "seq", "v", "i", "f", "fuel",
                        "oil", "t", "vb", "flt", "run", "rssi"]


def _reading(**kw):
    base = dict(v=0.0, i=0.0, f=0.0, fuel=85.0, oil=0, t=28.0, vb=12.6, rssi=-68)
    base.update(kw)
    return SensorReading(**base)


def _payload(**kw):
    reading = kw.pop("reading", _reading())
    args = dict(schema_version=1, device_id="ems-sim-01", boot=1, seq=1,
                reading=reading, flt=0, run=0, ts_ms=1_700_000_000_000)
    args.update(kw)
    return build_payload(**args)


def test_payload_matches_firmware_contract():
    p = _payload()
    assert list(p.keys()) == FIRMWARE_FIELD_ORDER
    assert set(SCHEMA_V1) == set(FIRMWARE_FIELD_ORDER)
    body = validate(p)
    assert json.loads(body) == p


def test_worst_case_payload_under_512_bytes():
    p = _payload(device_id="ems-sim-" + "9" * 20, boot=2**31, seq=2**31,
                 reading=_reading(v=259.987654, i=29.987654, f=50.123456,
                                  fuel=99.99999, oil=2, t=-12.3456, vb=13.87654, rssi=-120),
                 flt=2**31, run=4)
    assert len(validate(p).encode()) < 512


@pytest.mark.parametrize("field,value", [
    ("oil", 3), ("run", 5), ("fuel", 101.0), ("flt", -1), ("seq", "1"), ("oil", True),
])
def test_invalid_values_rejected(field, value):
    p = _payload()
    p[field] = value
    with pytest.raises(PayloadError):
        validate(p)


def test_missing_or_extra_field_rejected():
    p = _payload()
    del p["rssi"]
    with pytest.raises(PayloadError, match="missing"):
        validate(p)
    p = _payload()
    p["deviceId"] = "x"
    with pytest.raises(PayloadError, match="extra"):
        validate(p)


def test_size_limit_enforced():
    with pytest.raises(PayloadError, match="bytes"):
        validate(_payload(device_id="x" * 600))


def test_traceparent_format():
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", new_traceparent())


def test_off_state_sensor_baseline():
    model = SensorModel({"ambient_temp_c": 28, "battery_rest_v": 12.6,
                         "fuel_start_pct": 85}, rng=random.Random(1))
    for _ in range(200):
        r = model.read()
        assert (r.v, r.i, r.f) == (0.0, 0.0, 0.0)
        assert 27.5 <= r.t <= 28.5
        assert 12.5 <= r.vb <= 12.7
        assert -95 <= r.rssi <= -45
        validate(_payload(reading=r))
