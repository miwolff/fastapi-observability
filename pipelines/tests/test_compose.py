from pathlib import Path


def test_ofelia_jobs_use_a_five_minute_seconds_first_schedule() -> None:
    compose_file = Path(__file__).parents[2] / "docker-compose.yaml"
    compose = compose_file.read_text(encoding="utf-8")

    assert 'ofelia.job-run.enrich.schedule: "0 */5 * * * *"' in compose
    assert 'ofelia.job-run.score.schedule: "0 */5 * * * *"' in compose