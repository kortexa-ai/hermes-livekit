import copy
import json
from pathlib import Path

import pytest

from hermes_livekit.tool_result_protocol import (
    DRAIN_TIMEOUT_SEC,
    LIFECYCLE_FAILURE_CODES,
    BinaryResultProtocolError,
    bounded_next_size,
    drain_deadline_action,
    format_completed_result,
    parse_reference,
    release_stream,
    reserve_stream,
    stream_cancel_message,
    stream_ready_message,
    terminal_chunk_action,
    validate_completed_size,
    validate_stream_header,
)


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "binary_tool_results.json").read_text()
)


def _parse(case):
    return parse_reference(
        json.dumps(case["reference"]),
        rpc_owner_identity=case["rpc_owner_identity"],
        configured_timeout_sec=case["configured_timeout_sec"],
    )


@pytest.mark.parametrize("case", FIXTURES["valid"], ids=lambda case: case["name"])
def test_valid_references_are_bound_and_bounded(case):
    reference = _parse(case)
    assert reference.owner_identity == case["rpc_owner_identity"]
    assert reference.transfer_timeout_sec == case["expected_timeout_sec"]
    validate_stream_header(
        reference,
        sender_identity=reference.owner_identity,
        stream_id=reference.stream_id,
        topic=reference.topic,
        mime_type=reference.mime_type,
        total_size=reference.expected_size,
    )
    validate_completed_size(reference, reference.expected_size)


@pytest.mark.parametrize("invalid", FIXTURES["invalid"], ids=lambda case: case["name"])
def test_invalid_reference_fixtures_fail_closed(invalid):
    case = copy.deepcopy(FIXTURES["valid"][0])
    if "change" in invalid:
        key, value = invalid["change"]
        case["reference"][key] = value
    elif "remove" in invalid:
        del case["reference"][invalid["remove"]]
    else:
        key, value = invalid["add"]
        case["reference"][key] = value
    with pytest.raises(BinaryResultProtocolError) as error:
        _parse(case)
    assert error.value.code == invalid["error"]


def test_duplicate_json_fields_and_invalid_timeout_fail_closed():
    valid = FIXTURES["valid"][0]
    payload = json.dumps(valid["reference"])
    duplicate = payload[:-1] + ', "expected_size": 3}'
    with pytest.raises(BinaryResultProtocolError, match="invalid_reference"):
        parse_reference(
            duplicate,
            rpc_owner_identity=valid["rpc_owner_identity"],
            configured_timeout_sec=30,
        )
    with pytest.raises(BinaryResultProtocolError, match="invalid_timeout"):
        parse_reference(
            payload,
            rpc_owner_identity=valid["rpc_owner_identity"],
            configured_timeout_sec=float("inf"),
        )


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_version_requires_the_exact_integer_type(version):
    case = copy.deepcopy(FIXTURES["valid"][0])
    case["reference"]["version"] = version
    with pytest.raises(BinaryResultProtocolError, match="invalid_reference"):
        _parse(case)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sender_identity", "impostor"),
        ("stream_id", "a" * 32),
        ("topic", "other"),
        ("mime_type", "image/png"),
        ("total_size", None),
        ("total_size", 4),
    ],
)
def test_stream_header_must_match_rpc_reference(field, value):
    reference = _parse(FIXTURES["valid"][0])
    header = {
        "sender_identity": reference.owner_identity,
        "stream_id": reference.stream_id,
        "topic": reference.topic,
        "mime_type": reference.mime_type,
        "total_size": reference.expected_size,
    }
    header[field] = value
    with pytest.raises(BinaryResultProtocolError):
        validate_stream_header(reference, **header)


def test_incomplete_and_overlong_payloads_never_become_results():
    reference = _parse(FIXTURES["valid"][0])
    for size in (0, 2, 4):
        with pytest.raises(BinaryResultProtocolError, match="transfer_incomplete"):
            validate_completed_size(reference, size)
        with pytest.raises(BinaryResultProtocolError, match="transfer_incomplete"):
            format_completed_result(reference, b"x" * size)


def test_image_maps_to_hermes_multimodal_shape():
    reference = _parse(FIXTURES["valid"][0])
    result = format_completed_result(reference, b"jpg")
    assert result == {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": "Camera snapshot."},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64,anBn"},
            },
        ],
        "text_summary": "Camera snapshot.",
    }


def test_non_image_fallback_exposes_only_bounded_metadata():
    case = copy.deepcopy(FIXTURES["valid"][1])
    case["reference"]["expected_size"] = 3
    reference = _parse(case)
    result = format_completed_result(reference, b"pdf")
    assert result == {
        "binary_result": {
            "mime_type": "application/pdf",
            "size": 3,
            "available_to_model": False,
        },
        "text_summary": "The requested PDF was received.",
    }


def test_lifecycle_failure_fixture_is_the_closed_code_set():
    assert set(FIXTURES["lifecycle_failures"]) == LIFECYCLE_FAILURE_CODES


def test_ready_and_cancel_control_messages_match_fixtures():
    reference = _parse(FIXTURES["valid"][0])
    assert stream_ready_message(reference) == FIXTURES["control_messages"]["ready"]
    assert stream_cancel_message(reference) == FIXTURES["control_messages"]["cancel"]


def test_topic_is_globally_unique_while_outstanding_and_reusable_after_release():
    reference = _parse(FIXTURES["valid"][0])
    outstanding = set()
    assert reserve_stream(reference, outstanding) == reference.topic
    with pytest.raises(BinaryResultProtocolError) as error:
        reserve_stream(reference, outstanding)
    assert error.value.code == FIXTURES["reservation_collision_error"]
    release_stream(reference, outstanding)
    assert reserve_stream(reference, outstanding) == reference.topic


def test_overlong_sender_is_rejected_before_extra_bytes_are_buffered():
    reference = _parse(FIXTURES["valid"][0])
    received = bounded_next_size(reference, 0, 2)
    assert received == 2
    with pytest.raises(BinaryResultProtocolError) as error:
        bounded_next_size(reference, received, 2)
    assert error.value.code == FIXTURES["adversarial_sender"]["overlong_chunk_error"]


def test_ignored_cancel_has_bounded_non_accumulating_drain_and_escalation():
    policy = FIXTURES["adversarial_sender"]
    assert terminal_chunk_action() == policy["ignored_cancel_chunk_action"]
    assert DRAIN_TIMEOUT_SEC == policy["drain_timeout_sec"]
    assert (
        drain_deadline_action(
            trailer_received=False,
            deadline_generation=4,
            current_generation=4,
            replacement_started=False,
        )
        == policy["no_trailer_deadline_action"]
    )
    assert (
        drain_deadline_action(
            trailer_received=True,
            deadline_generation=4,
            current_generation=4,
            replacement_started=False,
        )
        == policy["trailer_deadline_action"]
    )
    assert (
        drain_deadline_action(
            trailer_received=False,
            deadline_generation=4,
            current_generation=4,
            replacement_started=True,
        )
        == policy["coalesced_deadline_action"]
    )
    assert (
        drain_deadline_action(
            trailer_received=False,
            deadline_generation=3,
            current_generation=4,
            replacement_started=False,
        )
        == policy["stale_deadline_action"]
    )
    assert policy["old_generation_failure"] in LIFECYCLE_FAILURE_CODES
