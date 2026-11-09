from pathlib import Path

import pytest

from pipelines.cli import build_parser


def test_cli_accepts_run_scoped_output_directory() -> None:
    arguments = build_parser().parse_args(
        ["enrich", "--input", "input.csv", "--output-dir", "output", "--run-id", "run-123"]
    )

    assert arguments.pipeline == "enrich"
    assert arguments.input == Path("input.csv")
    assert arguments.output is None
    assert arguments.output_dir == Path("output")
    assert arguments.run_id == "run-123"
    assert arguments.target == "latest"
    assert arguments.targets_file == Path("/app/config/targets.toml")


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "", "-leading-dash", "x" * 129])
def test_cli_rejects_unsafe_run_ids(run_id: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["enrich", "--input", "input.csv", "--output-dir", "output", "--run-id", run_id]
        )
