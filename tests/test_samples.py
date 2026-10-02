"""Every sample sheet in the catalog is valid against its own manifest,
its files exist, and the dates the example prompts name are the dates
the sheets hold."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from contracts.agent_samples import SAMPLES_FILENAME, SampleSheet

ROOT = Path(__file__).resolve().parent.parent
CATALOG = yaml.safe_load((ROOT / "decentai-agents.yaml").read_text(encoding="utf-8"))
AGENTS = {entry["id"]: ROOT / entry["path"] for entry in CATALOG["agents"]}
WITH_SAMPLES = sorted(agent_id for agent_id, folder in AGENTS.items()
                      if (folder / SAMPLES_FILENAME).is_file())


def _sheet(agent_id: str) -> SampleSheet:
    folder = AGENTS[agent_id]
    manifest = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
    sheet = SampleSheet.read(folder, manifest)
    assert sheet is not None
    return sheet


def test_the_agents_that_keep_records_ship_samples():
    assert WITH_SAMPLES == ["documents", "expenses", "json_data", "notebook",
                            "sheets", "slides", "tasks", "timesheets"]


@pytest.mark.parametrize("agent_id", WITH_SAMPLES)
def test_each_sheet_is_valid_against_its_manifest(agent_id):
    sheet = _sheet(agent_id)
    assert sheet.ok, "\n".join(sheet.errors)
    assert sheet.story
    assert sheet.records or sheet.files


def test_the_renewals_keep_their_notice_deadlines():
    """The lease the example prompt names: renews 1 March 2027 with 90
    days' notice, so the day to act is 1 December 2026."""
    tasks = _sheet("tasks")
    leases = [row for row in tasks.records
              if row["slot"] == "agreement" and row["fields"]["kind"] == "lease"]
    assert leases and leases[0]["fields"]["notice_deadline"] == "2026-12-01"
    assert leases[0]["fields"]["renewal_date"] == "2027-03-01"


def test_the_timesheet_week_has_one_short_day():
    """The week the example prompt names: 7 September 2026, every entry
    in its own ISO week, and by Thursday the 10th only Wednesday short."""
    by_day = {}
    for row in _sheet("timesheets").records:
        if row["slot"] != "entry":
            continue
        fields = row["fields"]
        year, week, _ = date.fromisoformat(fields["date"]).isocalendar()
        assert fields["week"] == f"{year}-W{week:02d}", fields
        by_day[fields["date"]] = by_day.get(fields["date"], Decimal(0)) + Decimal(fields["hours"])
    assert sorted(day for day, hours in by_day.items() if hours < 8) == ["2026-09-09"]


def test_every_sample_file_is_small_and_plain():
    for agent_id in WITH_SAMPLES:
        for item in _sheet(agent_id).files:
            path = AGENTS[agent_id] / item["path"]
            assert path.stat().st_size < 200_000, path
            assert path.suffix in (".md", ".txt", ".csv", ".pdf", ".json",
                                   ".pptx"), path
