"""变更流 API 行为测试。

- 上报/裁决改变结论（状态/当前分支/未齐祖先/有效值）时，在同一事务中
  按提交次序记录当时的完整结论快照（含删除标记）；
- 被拒绝的请求与纯重传不产生记录；
- 按序号游标限量领取，下一游标只越过已返回的记录，不重读、不漏读；
- 两个共库进程交错写入、翻页或重启后，旧记录仍保留原结论。
"""
import os
import sys
import threading

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def rev(obs, rid, parents, species=None, count=None, delete=False):
    body = {"observation_id": obs, "revision_id": rid, "parents": parents,
            "delete": delete}
    if species is not None:
        body["species"] = species
    if count is not None:
        body["count"] = count
    return body


def pkg(*revs):
    return {"revisions": list(revs)}


def changes(client, since=0, limit=100):
    r = client.get("/changes", params={"since": since, "limit": limit})
    assert r.status_code == 200
    return r.json()


def all_changes(client, limit=3):
    """按游标翻页收齐全部变更（小页幅强制多次翻页）。"""
    out, cursor = [], 0
    while True:
        page = changes(client, since=cursor, limit=limit)
        if not page["changes"]:
            assert page["next_cursor"] == cursor  # 空页不推进游标
            return out
        out.extend(page["changes"])
        cursor = page["next_cursor"]


# ------------------------------------------------------------ 记录内容与时机

def test_every_transition_records_full_snapshot(client):
    # 子先父后：pending（不出有效值）
    client.post("/packages", json=pkg(rev("O1", "r2", ["r1"], species="B", count=2)))
    # 父补齐：自动重算为 valid，有效值取顶端 r2
    client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=1)))
    # 并发删除分支：conflict（旧值被撤回，不再输出有效值）
    client.post("/packages", json=pkg(rev("O1", "r3", ["r1"], delete=True)))
    # 人工裁决：valid
    r = client.post("/observations/O1/resolutions",
                    json={"revision_id": "r4", "parents": ["r2", "r3"],
                          "species": "A", "count": 5})
    assert r.status_code == 201

    recs = changes(client)["changes"]
    assert [c["seq"] for c in recs] == [1, 2, 3, 4]  # 提交次序，连续不重不漏

    assert recs[0]["observation_id"] == "O1"
    assert recs[0]["status"] == "pending"
    assert recs[0]["tips"] == ["r2"]
    assert recs[0]["missing_ancestors"] == ["r1"]
    assert recs[0]["effective"] is None
    assert recs[0]["version"] == 1

    assert recs[1]["status"] == "valid"
    assert recs[1]["tips"] == ["r2"]
    assert recs[1]["missing_ancestors"] == []
    assert recs[1]["effective"] == {"revision_id": "r2", "species": "B",
                                    "count": 2, "deleted": False}
    assert recs[1]["version"] == 2

    assert recs[2]["status"] == "conflict"
    assert recs[2]["tips"] == ["r2", "r3"]
    assert recs[2]["effective"] is None
    assert recs[2]["version"] == 3

    assert recs[3]["status"] == "valid"
    assert recs[3]["tips"] == ["r4"]
    assert recs[3]["effective"]["count"] == 5
    assert recs[3]["version"] == 4
    assert all(c["recorded_at"] for c in recs)


def test_delete_marker_recorded(client):
    client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=1)))
    client.post("/packages", json=pkg(rev("O1", "r2", ["r1"], delete=True)))
    recs = changes(client)["changes"]
    assert len(recs) == 2
    assert recs[1]["status"] == "valid"
    assert recs[1]["effective"] == {"revision_id": "r2", "species": None,
                                    "count": None, "deleted": True}


def test_rejected_and_pure_retransmit_produce_no_records(client):
    p1 = pkg(rev("O1", "r1", [], species="A", count=1))
    client.post("/packages", json=p1)
    client.post("/packages", json=pkg(rev("O2", "r9", [], species="B", count=2)))
    assert len(changes(client)["changes"]) == 2

    # 纯重传：同号同内容 → duplicates，无新记录
    r = client.post("/packages", json=p1)
    assert r.json()["duplicates"] == ["r1"]
    # 同号异内容 → 422 整包拒绝，无记录
    assert client.post("/packages", json=pkg(
        rev("O1", "r1", [], species="A", count=9))).status_code == 422
    # 循环引用 → 422 整包拒绝，无记录
    assert client.post("/packages", json=pkg(
        rev("O3", "c1", ["c2"], species="C", count=1),
        rev("O3", "c2", ["c1"], species="C", count=1),
    )).status_code == 422
    assert len(changes(client)["changes"]) == 2

    # O2 出现并发分支 → conflict，仅此一条新记录
    client.post("/packages", json=pkg(rev("O2", "r8", [], species="B", count=3)))
    assert len(changes(client)["changes"]) == 3
    # 裁决 409（提交时分支已变）→ 无记录
    r = client.post("/observations/O2/resolutions",
                    json={"revision_id": "res1", "parents": ["r9"],
                          "species": "B", "count": 7})
    assert r.status_code == 409
    assert len(changes(client)["changes"]) == 3
    # 裁决成功 → 1 条；同内容重放（200）→ 无记录；同号异内容（422）→ 无记录
    body = {"revision_id": "res1", "parents": ["r8", "r9"], "species": "B", "count": 7}
    assert client.post("/observations/O2/resolutions", json=body).status_code == 201
    assert len(changes(client)["changes"]) == 4
    assert client.post("/observations/O2/resolutions", json=body).status_code == 200
    assert client.post("/observations/O2/resolutions",
                       json=dict(body, count=8)).status_code == 422
    # 未知观测裁决 404 → 无记录
    assert client.post("/observations/NOPE/resolutions",
                       json={"revision_id": "x1", "parents": [],
                             "species": "A", "count": 1}).status_code == 404
    recs = changes(client)["changes"]
    assert [c["seq"] for c in recs] == [1, 2, 3, 4]


# ------------------------------------------------------------ 游标与翻页

def test_cursor_pagination_skips_only_returned(client):
    for i in range(5):
        client.post("/packages", json=pkg(
            rev(f"P{i}", f"p{i}-r1", [], species="A", count=i)))

    page1 = changes(client, since=0, limit=2)
    assert [c["seq"] for c in page1["changes"]] == [1, 2]
    assert page1["next_cursor"] == 2  # 只越过已返回的 1、2

    page2 = changes(client, since=page1["next_cursor"], limit=2)
    assert [c["seq"] for c in page2["changes"]] == [3, 4]
    assert page2["next_cursor"] == 4

    page3 = changes(client, since=page2["next_cursor"], limit=2)
    assert [c["seq"] for c in page3["changes"]] == [5]
    assert page3["next_cursor"] == 5  # 本页只返回一条，游标只越过这一条

    page4 = changes(client, since=page3["next_cursor"], limit=2)
    assert page4 == {"changes": [], "next_cursor": 5}  # 空页不推进游标

    # 游标语义为“严格大于”：不重读
    again = changes(client, since=4, limit=100)
    assert [c["seq"] for c in again["changes"]] == [5]


def test_changes_query_validation(client):
    assert client.get("/changes", params={"since": -1}).status_code == 422
    assert client.get("/changes", params={"limit": 0}).status_code == 422
    assert client.get("/changes", params={"limit": 1001}).status_code == 422
    assert client.get("/changes").status_code == 200  # 默认 since=0&limit=100


def test_changes_endpoint_in_openapi(client):
    spec = client.get("/openapi.json").json()
    assert "/changes" in spec["paths"]


# ------------------------------------------------------------ 不可变与重启

def test_old_records_keep_original_conclusion_after_restart(client):
    client.post("/packages", json=pkg(rev("O1", "r1", [], species="A", count=1)))
    client.post("/packages", json=pkg(rev("O1", "r2", ["r1"], species="A", count=2)))
    before = changes(client)["changes"]

    # 后续变更：改值被删除标记取代
    client.post("/packages", json=pkg(rev("O1", "r3", ["r2"], delete=True)))

    # 模拟进程重启：同一库文件新建应用实例，旧记录仍保留原结论
    with TestClient(app) as restarted:
        recs = changes(restarted)["changes"]
    assert recs[:2] == before
    assert recs[0]["effective"]["count"] == 1
    assert recs[1]["effective"]["count"] == 2
    assert recs[2]["effective"]["deleted"] is True
    # 当前结论已变，但旧记录仍是当时的结论（分析库可据此撤回失效值）
    assert client.get("/observations/O1").json()["effective"]["deleted"] is True


# ------------------------------------------------------------ 双进程共库

def test_interleaved_writers_two_processes_consistent_stream(client):
    with TestClient(app) as other:  # 第二个共库进程（同一库文件）
        # 两端交错写入
        client.post("/packages", json=pkg(rev("OA", "a1", [], species="A", count=1)))
        other.post("/packages", json=pkg(rev("OB", "b1", [], species="B", count=1)))
        client.post("/packages", json=pkg(rev("OA", "a2", ["a1"], species="A", count=2)))
        other.post("/packages", json=pkg(rev("OB", "b2", ["b1"], delete=True)))

        from_a = all_changes(client, limit=1)  # 从 a 端逐条翻页
        from_b = all_changes(other, limit=2)   # 从 b 端用另一页幅翻页
    assert from_a == from_b                    # 两端领取到的流一致
    assert [c["seq"] for c in from_a] == [1, 2, 3, 4]
    assert [c["observation_id"] for c in from_a] == ["OA", "OB", "OA", "OB"]


def test_concurrent_writers_no_missing_or_duplicate_records(client):
    n = 12
    errors = []

    def writer(c, obs, prefix):
        try:
            for i in range(n):
                parents = [f"{prefix}{i - 1}"] if i else []
                r = c.post("/packages", json=pkg(
                    rev(obs, f"{prefix}{i}", parents, species="A", count=i)))
                assert r.status_code == 200, r.text
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    with TestClient(app) as other:
        t1 = threading.Thread(target=writer, args=(client, "OT1", "t1-"))
        t2 = threading.Thread(target=writer, args=(other, "OT2", "t2-"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    assert not errors

    recs = all_changes(client, limit=5)
    seqs = [c["seq"] for c in recs]
    assert len(seqs) == 2 * n
    assert len(set(seqs)) == len(seqs)        # 不重读
    assert seqs == sorted(seqs)               # 按提交次序领取
    assert seqs == list(range(1, 2 * n + 1))  # 不漏读（序号连续）
    for obs in ("OT1", "OT2"):                # 每个观测的版本按序递增且完整
        versions = [c["version"] for c in recs if c["observation_id"] == obs]
        assert versions == list(range(1, n + 1))
