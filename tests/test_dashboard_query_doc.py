import json

from tools import dashboard_query_doc as q


def test_every_waste_dataset_has_a_note():
    dash = json.loads(q.DASH.read_text(encoding="utf-8"))
    assert [d["displayName"] for d in dash["datasets"] if d["displayName"] not in q.NOTES] == []


def test_comments_leave_the_sql_unchanged():
    dash = json.loads(q.DASH.read_text(encoding="utf-8"))
    for d in dash["datasets"]:
        sql = "".join(d["queryLines"])
        kept = [ln for ln in q.commented(sql).split("\n") if not ln.lstrip().startswith("-- ")]
        assert "".join("".join(kept).split()) == "".join(sql.split()), d["displayName"]


def test_doc_is_current():
    assert q.OUT.read_text(encoding="utf-8") == q.build()
