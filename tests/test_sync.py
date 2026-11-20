import json
import sqlite3
from datetime import UTC, datetime

import pytest

from cmdbsync.cli import main
from cmdbsync.sources import (
    AwsConfigSource,
    AzureResourceGraphSource,
    GcpAssetInventorySource,
    SourceError,
)
from cmdbsync.store import ServiceNowSink, SqliteSink, SyncRefused

T1 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
ACCOUNT = "123456789012"


def item(rid, rtype="AWS::EC2::Instance", name="", tags=None, region="eu-west-2"):
    return json.dumps(
        {
            "resourceId": rid,
            "resourceType": rtype,
            "resourceName": name,
            "awsRegion": region,
            "accountId": ACCOUNT,
            "arn": f"arn:aws:ec2:{region}:{ACCOUNT}:instance/{rid}",
            "tags": [{"key": k, "value": v} for k, v in (tags or {}).items()],
        }
    )


class FakeConfig:
    """select_resource_config with NextToken paging over JSON-string results."""

    def __init__(self, results, page_size=2):
        self.results = list(results)
        self.page_size = page_size
        self.calls = []

    def select_resource_config(self, Expression, NextToken=None):
        self.calls.append({"Expression": Expression, "NextToken": NextToken})
        start = int(NextToken or 0)
        page = {"Results": self.results[start : start + self.page_size]}
        if start + self.page_size < len(self.results):
            page["NextToken"] = str(start + self.page_size)
        return page


def fleet():
    return [
        item("i-0000000000000001", name="web-1", tags={"env": "prod"}),
        item("i-0000000000000002", name="web-2", tags={"env": "prod"}),
        item("i-0000000000000003", name="batch", tags={"env": "dev"}),
        item("reports-example-com", rtype="AWS::S3::Bucket"),
    ]


def run(db, results, *extra, now=T1):
    lines = []
    code = main(
        ["--db", str(db), "--type", "AWS::EC2::Instance", "--type", "AWS::S3::Bucket", *extra],
        config_client=FakeConfig(results),
        now=now,
        out=lines.append,
    )
    return code, lines


def rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT * FROM resources ORDER BY key").fetchall()
    finally:
        conn.close()


def by_id(db):
    return {r[4]: r for r in rows(db)}


# --- source ---------------------------------------------------------------------


def test_reads_every_page_and_builds_query():
    fake = FakeConfig(fleet(), page_size=3)
    records = AwsConfigSource(fake, ["AWS::EC2::Instance"]).fetch()
    assert len(records) == 4
    assert [c["NextToken"] for c in fake.calls] == [None, "3"]
    assert "WHERE resourceType IN ('AWS::EC2::Instance')" in fake.calls[0]["Expression"]


def test_tags_are_order_independent():
    a = AwsConfigSource(
        FakeConfig([item("i-1", tags={"a": "1", "b": "2"})]), ["AWS::EC2::Instance"]
    )
    b_raw = json.loads(item("i-1"))
    b_raw["tags"] = [{"key": "b", "value": "2"}, {"key": "a", "value": "1"}]
    b = AwsConfigSource(FakeConfig([json.dumps(b_raw)]), ["AWS::EC2::Instance"])
    assert a.fetch()[0].content_hash == b.fetch()[0].content_hash


@pytest.mark.parametrize("bad", ["AWS::EC2::Instance'); DROP", "EC2::Instance", ""])
def test_resource_type_is_validated(bad):
    with pytest.raises(ValueError):
        AwsConfigSource(FakeConfig([]), [bad])


def test_unusable_result_fails():
    with pytest.raises(SourceError):
        AwsConfigSource(FakeConfig(['{"resourceId": "x"}']), ["AWS::EC2::Instance"]).fetch()


def test_stubs_are_not_implemented(tmp_path):
    for stub in (AzureResourceGraphSource(), GcpAssetInventorySource()):
        with pytest.raises(NotImplementedError):
            stub.fetch()
    with pytest.raises(NotImplementedError):
        ServiceNowSink().plan([], False)


# --- sync -------------------------------------------------------------------------


def test_first_run_inserts_everything(tmp_path):
    db = tmp_path / "cmdb.sqlite"
    code, lines = run(db, fleet())
    assert code == 0
    assert lines[-2] == "4 resources from AWS Config; 4 new, 0 changed, 0 revived, 0 retired"
    assert {r[9] for r in rows(db)} == {"active"}


def test_second_run_changes_nothing(tmp_path):
    db = tmp_path / "cmdb.sqlite"
    run(db, fleet())
    before = rows(db)
    code, lines = run(db, fleet(), now=T2)
    assert code == 0
    assert "0 new, 0 changed, 0 revived, 0 retired" in lines[-2]
    assert lines[-1] == "nothing to do"
    assert rows(db) == before  # not even a timestamp moved


def test_changed_tag_updates_one_row(tmp_path):
    db = tmp_path / "cmdb.sqlite"
    run(db, fleet())
    changed = fleet()
    changed[0] = item("i-0000000000000001", name="web-1", tags={"env": "staging"})
    code, lines = run(db, changed, now=T2)
    assert "~ " + f"{ACCOUNT}/eu-west-2/AWS::EC2::Instance/i-0000000000000001" in lines
    row = by_id(db)["i-0000000000000001"]
    assert json.loads(row[7]) == [["env", "staging"]]
    assert row[10] == T1.isoformat()  # first_seen kept
    assert row[11] == T2.isoformat()  # updated_at moved
    assert by_id(db)["i-0000000000000002"][11] == T1.isoformat()


def test_missing_resource_is_soft_retired_not_deleted(tmp_path):
    db = tmp_path / "cmdb.sqlite"
    run(db, fleet())
    code, _ = run(db, fleet()[1:], now=T2)
    assert code == 0
    assert len(rows(db)) == 4
    gone = by_id(db)["i-0000000000000001"]
    assert gone[9] == "retired"
    assert gone[12] == T2.isoformat()
    # Still missing next time: no further change.
    before = rows(db)
    run(db, fleet()[1:], now=datetime(2026, 11, 22, tzinfo=UTC))
    assert rows(db) == before


def test_returning_resource_is_revived(tmp_path):
    db = tmp_path / "cmdb.sqlite"
    run(db, fleet())
    run(db, fleet()[1:], now=T2)
    code, lines = run(db, fleet(), now=datetime(2026, 11, 22, tzinfo=UTC))
    assert any(line.startswith("^ ") for line in lines)
    row = by_id(db)["i-0000000000000001"]
    assert row[9] == "active" and row[12] is None


def test_dry_run_prints_diff_and_writes_nothing(tmp_path):
    db = tmp_path / "cmdb.sqlite"
    code, lines = run(db, fleet(), "--dry-run")
    assert code == 0
    assert not db.exists()
    assert sum(line.startswith("+ ") for line in lines) == 4
    assert lines[-1] == "dry-run: database not changed"

    run(db, fleet())
    before = rows(db)
    _, lines = run(db, fleet()[:2], "--dry-run", now=T2)
    assert sum(line.startswith("- ") for line in lines) == 2
    assert rows(db) == before


def test_empty_source_refused_when_db_has_active_rows(tmp_path, capsys):
    db = tmp_path / "cmdb.sqlite"
    run(db, fleet())
    before = rows(db)
    code, _ = run(db, [], now=T2)
    assert code == 3
    assert "refusing to retire them all" in capsys.readouterr().err
    assert rows(db) == before


def test_empty_source_allowed_with_flag(tmp_path):
    db = tmp_path / "cmdb.sqlite"
    run(db, fleet())
    code, _ = run(db, [], "--allow-empty-source", now=T2)
    assert code == 0
    assert {r[9] for r in rows(db)} == {"retired"}


def test_duplicate_key_from_source_fails(tmp_path, capsys):
    db = tmp_path / "cmdb.sqlite"
    code, _ = run(db, [item("i-1"), item("i-1")])
    assert code == 1
    assert "twice" in capsys.readouterr().err
    assert not db.exists()


def test_same_id_in_two_regions_are_two_rows(tmp_path):
    db = tmp_path / "cmdb.sqlite"
    run(db, [item("i-1", region="eu-west-2"), item("i-1", region="us-east-1")])
    assert len(rows(db)) == 2


def test_api_error_exits_1(tmp_path, capsys):
    class Broken(FakeConfig):
        def select_resource_config(self, **kw):
            raise RuntimeError("AccessDenied")

    code = main(
        ["--db", str(tmp_path / "c.sqlite"), "--type", "AWS::EC2::Instance"],
        config_client=Broken([]),
        out=lambda s: None,
    )
    assert code == 1
    assert "AccessDenied" in capsys.readouterr().err


def test_apply_is_all_or_nothing(tmp_path):
    db = tmp_path / "cmdb.sqlite"
    sink = SqliteSink(db)
    records = AwsConfigSource(FakeConfig(fleet()), ["AWS::EC2::Instance"]).fetch()
    plan = sink.plan(records, False)
    plan.insert.append(plan.insert[0])  # duplicate primary key mid-transaction
    with pytest.raises(sqlite3.IntegrityError):
        sink.apply(plan, T1)
    assert rows(db) == []


def test_refusal_type():
    from cmdbsync.store import build_plan

    with pytest.raises(SyncRefused):
        build_plan([], {"k": ("h", "active")}, allow_empty_source=False)
