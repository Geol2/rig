"""`inputs` (rig run -i name=value) and per-hand `output.schema` (structured outputs)."""

import asyncio
import json

import pytest
from pydantic import ValidationError

from rig.cli import main
from rig.hand import ClaudeWorker, EchoWorker
from rig.runner import build_prompt, run_shift
from rig.spec import InputError, Rig
from rig.tools import Toolbox

INPUTS = {
    "dataset": {"description": "CSV to analyze", "required": True},
    "audience": {"default": "executives"},
    "weeks": {"type": "integer", "default": 4},
    "draft": {"type": "boolean", "default": False},
    "note": {},
}


def make(**overrides):
    data = {
        "name": "t",
        "inputs": INPUTS,
        "hands": {
            "analyst": {"role": "Analyze {{ inputs.dataset }} over {{inputs.weeks}} weeks.{{ inputs.note }}"},
            "writer": {"role": "Write for {{ inputs.audience }}."},
        },
        "lines": ["analyst -> writer"],
    }
    data.update(overrides)
    return Rig.model_validate(data)


def test_resolve_inputs_fills_defaults_and_normalizes():
    values = make().resolve_inputs({"dataset": "sales.csv", "weeks": "08", "draft": "YES"})
    assert values == {"dataset": "sales.csv", "audience": "executives", "weeks": "8", "draft": "true"}


@pytest.mark.parametrize(
    "given, message",
    [
        ({}, "missing required inputs: dataset"),
        ({"dataset": "x", "colour": "red"}, "unknown inputs ['colour']"),
        ({"dataset": "x", "weeks": "four"}, "'weeks' must be an integer"),
        ({"dataset": "x", "draft": "maybe"}, "'draft' must be true or false"),
    ],
)
def test_resolve_inputs_errors(given, message):
    with pytest.raises(InputError, match=message.replace("[", r"\[").replace("]", r"\]")):
        make().resolve_inputs(given)


def test_default_must_fit_type():
    with pytest.raises(ValidationError, match="must be an integer"):
        make(inputs={"weeks": {"type": "integer", "default": "soon"}})


def test_roles_get_input_values():
    rig = make()
    values = rig.resolve_inputs({"dataset": "sales.csv"})
    # An optional input left unset reads as empty.
    assert rig.resolve("analyst", values).role == "Analyze sales.csv over 4 weeks."
    assert rig.resolve("writer", values).role == "Write for executives."
    # Without values, references are left empty.
    assert "{{" not in rig.resolve("writer").role


def test_undeclared_input_in_role_rejected():
    with pytest.raises(ValidationError, match=r"writer's role uses undeclared inputs \['tone'\]"):
        make(hands={"analyst": {"role": "A"}, "writer": {"role": "Use a {{ inputs.tone }} tone."}})


def test_prompt_has_inputs_block():
    prompt = build_prompt("do it", {"a": "out"}, inputs={"dataset": "sales.csv"})
    assert prompt == (
        '<task>\ndo it\n</task>\n\n<inputs>\n<input name="dataset">sales.csv</input>\n</inputs>\n\n'
        '<handoff from="a">\nout\n</handoff>'
    )
    assert "<inputs>" not in build_prompt("do it", {})


def test_shift_passes_inputs_to_every_hand(tmp_path):
    shift = asyncio.run(run_shift(make(), "report", EchoWorker(), root=tmp_path, on_event=lambda _: None,
                                  inputs={"dataset": "sales.csv"}))
    assert shift.ok
    for res in shift.results.values():
        assert '<input name="dataset">sales.csv</input>' in res.output
    summary = json.loads((shift.dir / "shift.json").read_text(encoding="utf-8"))
    assert summary["inputs"]["audience"] == "executives"


def test_shift_rejects_bad_inputs_before_starting(tmp_path):
    with pytest.raises(InputError):
        asyncio.run(run_shift(make(), "report", EchoWorker(), root=tmp_path, on_event=lambda _: None))
    assert not (tmp_path / ".rig").exists()


def test_foreman_sees_inputs(tmp_path):
    rig = Rig.model_validate({
        "name": "t",
        "inputs": {"dataset": {"required": True}},
        "foreman": {"role": "Lead the analysis of {{ inputs.dataset }}."},
        "hands": {"analyst": {"role": "Analyze."}},
    })
    shift = asyncio.run(run_shift(rig, "report", EchoWorker(), root=tmp_path, on_event=lambda _: None,
                                  inputs={"dataset": "sales.csv"}))
    assert '<input name="dataset">sales.csv</input>' in shift.final.output


# --- output.schema --------------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "kpis": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}},
        "summary": {"type": "string"},
    },
    "required": ["kpis", "summary"],
}


def test_schema_objects_are_closed():
    rig = make(hands={"analyst": {"role": "A", "output": {"schema": SCHEMA}}, "writer": {"role": "W"}})
    schema = rig.resolve("analyst").output_schema
    assert schema["additionalProperties"] is False
    assert schema["properties"]["kpis"]["items"]["additionalProperties"] is False
    assert rig.resolve("writer").output_schema is None


def test_open_schema_rejected():
    with pytest.raises(ValidationError, match=r"schema\.properties\.meta: additionalProperties must be false"):
        make(hands={"analyst": {"role": "A", "output": {"schema": {
            "type": "object", "properties": {"meta": {"type": "object", "additionalProperties": True}},
        }}}, "writer": {"role": "W"}})


def test_worker_requests_structured_output(tmp_path):
    from test_worker import FakeClient, block, response

    rig = make(hands={"analyst": {"role": "A", "output": {"schema": SCHEMA}}, "writer": {"role": "W"}})
    hand = rig.resolve("analyst")
    client = FakeClient([response("end_turn", block("text", text='{"kpis": [], "summary": "flat"}'))])
    res = asyncio.run(ClaudeWorker(client).run(hand, "go", Toolbox(tmp_path, [])))
    assert json.loads(res.output) == {"kpis": [], "summary": "flat"}
    assert client.calls[0]["output_config"] == {
        "effort": "medium", "format": {"type": "json_schema", "schema": hand.output_schema},
    }


def test_worker_without_schema_sends_no_format(tmp_path):
    from test_worker import FakeClient, block, response

    client = FakeClient([response("end_turn", block("text", text="hi"))])
    asyncio.run(ClaudeWorker(client).run(make().resolve("writer"), "go", Toolbox(tmp_path, [])))
    assert client.calls[0]["output_config"] == {"effort": "medium"}


# --- CLI ------------------------------------------------------------------------------------------

RIG_YAML = """\
name: report
inputs:
  dataset: {required: true}
  weeks: {type: integer, default: 4}
hands:
  analyst: {role: "Analyze {{ inputs.dataset }}."}
"""


def test_cli_check_lists_inputs(tmp_path, monkeypatch, capsys):
    (tmp_path / "rig.yaml").write_text(RIG_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    main(["check"])
    assert "inputs: dataset (required), weeks (default 4)" in capsys.readouterr().out


def test_cli_run_with_inputs(tmp_path, monkeypatch, capsys):
    (tmp_path / "rig.yaml").write_text(RIG_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    main(["run", "--dry", "-q", "-i", "dataset=a=b.csv", "--input", "weeks=2", "go"])
    out = capsys.readouterr().out
    assert '<input name="dataset">a=b.csv</input>' in out and '<input name="weeks">2</input>' in out


@pytest.mark.parametrize(
    "args, message",
    [
        (["-i", "dataset"], "bad input 'dataset'"),
        ([], "missing required inputs: dataset"),
        (["-i", "dataset=x", "-i", "weeks=two"], "'weeks' must be an integer"),
    ],
)
def test_cli_run_input_errors(tmp_path, monkeypatch, args, message):
    (tmp_path / "rig.yaml").write_text(RIG_YAML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["run", "--dry", *args, "go"])
    assert message in excinfo.value.code
    assert not (tmp_path / ".rig").exists()
