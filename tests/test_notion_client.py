"""Notion の通信障害注入。財務データではなく再送回数と既存失敗経路を検証する。"""

import json
from unittest.mock import Mock

import httpx
import pytest
import requests

from jp_stock_pipeline.notion import client as module


@pytest.fixture
def client(monkeypatch):
    c = module.NotionClient("test-token-not-a-credential")
    monkeypatch.setattr(c._throttle, "wait", lambda: None)
    monkeypatch.setattr(module.time, "sleep", Mock())
    # 不意の実通信を禁止。各テストで必要な通信ダブルだけを設定する。
    monkeypatch.setattr(c._client.client, "send", Mock(side_effect=AssertionError("実通信禁止")))
    monkeypatch.setattr(c._session, "request", Mock(side_effect=AssertionError("実通信禁止")))
    yield c
    c._client.close()
    c._session.close()


def sdk_responses(client, outcomes):
    calls = []
    replies = iter(outcomes)

    def send(request):
        calls.append(request)
        result = next(replies)
        if isinstance(result, Exception):
            raise result
        status, body, headers = result
        return httpx.Response(status, json=body, headers=headers, request=request)

    client._client.client.send = send
    return calls


def raw_response(status, body=None, headers=None):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body or {"id": "result"}).encode()
    response.headers.update(headers or {})
    return response


CREATES = [
    lambda c: c.create_page(parent={"database_id": "db"}, properties={}),
    lambda c: c.create_database(parent_page_id="parent", title="test", properties={}),
    lambda c: c.append_block_children("parent", [{"object": "block"}]),
]


@pytest.mark.parametrize("operation", CREATES, ids=["page", "database", "append"])
@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout("response lost after server accepted request"),
    httpx.ConnectError("connection lost"),
    (503, {"code": "service_unavailable", "message": "unavailable"}, {}),
    (502, {"unexpected": "unstructured error"}, {}),
])
def test_creation_ambiguous_result_is_sent_once(client, operation, failure):
    calls = sdk_responses(client, [failure, (200, {"id": "duplicate"}, {})])
    with pytest.raises(module.NotionRequestError, match="結果不明.*自動再送しません"):
        operation(client)
    assert len(calls) == 1
    module.time.sleep.assert_not_called()


@pytest.mark.parametrize("operation", CREATES, ids=["page", "database", "append"])
@pytest.mark.parametrize("status,code", [(429, "rate_limited"), (529, "service_overload")])
def test_creation_rate_limit_still_retries_with_retry_after(client, operation, status, code):
    calls = sdk_responses(client, [
        (status, {"code": code, "message": "wait"}, {"Retry-After": "7"}),
        (200, {"id": "created-once"}, {}),
    ])
    assert operation(client)["id"] == "created-once"
    assert len(calls) == 2
    module.time.sleep.assert_called_once_with(7.0)


@pytest.mark.parametrize("operation", [
    lambda c: c.query_database("db"),  # SDK は POST /databases/db/query を送信する。
    lambda c: c.get_page("page"),
    lambda c: c.retrieve_database("db"),
    lambda c: c.list_child_blocks("block"),
    lambda c: c.update_page("page", {}),
    lambda c: c.update_database("db", properties={}),
    lambda c: c.archive_page("page"),  # 何度 archive しても同じ結果なので再試行してよい
], ids=["query-post", "get-page", "get-db", "list-blocks", "update-page", "update-db",
        "archive-page"])
def test_reads_and_property_overwrites_still_recover(client, operation):
    calls = sdk_responses(client, [
        httpx.ReadTimeout("timeout"),
        (503, {"code": "service_unavailable", "message": "wait"}, {"Retry-After": "4"}),
        (200, {"results": [], "id": "existing"}, {}),
    ])
    operation(client)
    assert len(calls) == 3
    assert [call.args[0] for call in module.time.sleep.call_args_list] == [1.0, 4.0]


@pytest.mark.parametrize("method,path", [
    ("POST", "pages"), ("POST", "file_uploads"),
    ("POST", "file_uploads/upload/send"), ("POST", "file_uploads/upload/complete"),
    ("PATCH", "blocks/block/children"),
])
@pytest.mark.parametrize("failure", [requests.Timeout("response lost"), raw_response(503)])
def test_raw_writes_never_repeat_ambiguous_result(client, method, path, failure):
    client._session.request.side_effect = [failure, raw_response(200)]
    with pytest.raises(module.NotionRequestError, match="結果不明"):
        client.raw_api(method, path, json_body={})
    assert client._session.request.call_count == 1
    module.time.sleep.assert_not_called()


@pytest.mark.parametrize("status", [429, 529])
def test_raw_creation_rate_limits_keep_retry_after(client, status):
    client._session.request.side_effect = [
        raw_response(status, headers={"Retry-After": "9"}), raw_response(200),
    ]
    assert client.raw_api("POST", "file_uploads")["id"] == "result"
    assert client._session.request.call_count == 2
    module.time.sleep.assert_called_once_with(9.0)


@pytest.mark.parametrize("method,path", [
    ("GET", "pages/page"), ("POST", "search"),
    ("POST", "databases/db/query"), ("POST", "/data_sources/source/query/"),
])
def test_raw_reads_including_post_recover(client, method, path):
    client._session.request.side_effect = [
        requests.Timeout("timeout"), raw_response(503), raw_response(200),
    ]
    assert client.raw_api(method, path)["id"] == "result"
    assert client._session.request.call_count == 3


def test_rate_limit_then_unknown_creation_result_does_not_send_third_request(client):
    calls = sdk_responses(client, [
        (429, {"code": "rate_limited", "message": "wait"}, {}),
        httpx.ReadTimeout("accepted but response lost"),
        (200, {"id": "duplicate"}, {}),
    ])
    with pytest.raises(module.NotionRequestError, match="結果不明"):
        CREATES[0](client)
    assert len(calls) == 2


def test_api_validation_error_keeps_original_type_without_retry(client):
    from notion_client.errors import APIResponseError

    calls = sdk_responses(client, [
        (400, {"code": "validation_error", "message": "invalid"}, {}),
    ])
    with pytest.raises(APIResponseError):
        CREATES[0](client)
    assert len(calls) == 1


def test_dry_run_creation_still_records_without_network(dry_client):
    for operation in CREATES:
        assert operation(dry_client)["id"].startswith("dry-run-")
    assert dry_client.raw_api("POST", "file_uploads")["id"].startswith("dry-run-")
    assert len(dry_client.ops) == 4


def test_archive_page_sends_archived_flag_only(client):
    """archive は復元可能な削除（archived=true）だけを送り、プロパティは触らない (#13)。"""
    calls = sdk_responses(client, [(200, {"id": "page", "archived": True}, {})])
    client.archive_page("page")
    assert len(calls) == 1
    assert calls[0].method == "PATCH"
    assert json.loads(calls[0].content) == {"archived": True}


def test_dry_run_archive_is_recorded_without_network(dry_client):
    dry_client.archive_page("page")
    assert [(op.op, op.payload) for op in dry_client.ops] == [
        ("archive_page", {"page_id": "page"})
    ]


class TestPostJsonErrorDetail:
    """4xx の応答本文を握り潰さない。どの権限が足りないかは本文にしか無い。"""

    def test_body_is_included_in_the_error(self, monkeypatch):
        import requests

        from jp_stock_pipeline import http

        class _Resp:
            status_code = 403
            text = '{"success":false,"errors":[{"code":7403,"message":"Unauthorized"}]}'

        monkeypatch.setattr(
            requests.Session, "post", lambda self, url, **kw: _Resp()
        )
        with pytest.raises(http.FetchError, match="7403"):
            http.post_json("https://example/api", json_body={})

    def test_unreadable_body_does_not_mask_the_status(self, monkeypatch):
        import requests

        from jp_stock_pipeline import http

        class _Resp:
            status_code = 500

            @property
            def text(self):
                raise RuntimeError("decode failed")

        monkeypatch.setattr(
            requests.Session, "post", lambda self, url, **kw: _Resp()
        )
        with pytest.raises(http.FetchError, match="HTTP 500"):
            http.post_json("https://example/api", json_body={})

class TestQueryTruncation:
    """Notion の 10,000 件打ち切りを「全件取れた」と誤認しない (§3-2)。

    上限に達すると has_more が false になるので、素直に読むと欠損に気付けない。
    ⑥エクスポートは全件を1ファイルに固めるため、部分データを完全なデータセットと
    して配ると事実誤認を配ることになる。
    """

    def _client(self, monkeypatch, resp):
        from jp_stock_pipeline.notion.client import NotionClient

        client = NotionClient("tok", rps=1000.0, dry_run=False)

        class _DB:
            def query(self, **kwargs):
                return resp

        class _Inner:
            databases = _DB()

        client._client = _Inner()  # noqa: SLF001
        return client

    def test_incomplete_marker_raises_in_strict_mode(self, monkeypatch):
        from jp_stock_pipeline.notion.client import QueryTruncatedError

        client = self._client(monkeypatch, {
            "results": [{"id": "x"}],
            "has_more": False,
            "request_status": {
                "type": "incomplete", "incomplete_reason": "query_result_limit_reached"
            },
        })
        with pytest.raises(QueryTruncatedError, match="打ち切られた"):
            client.query_database("db", strict=True)

    def test_reaching_the_cap_is_detected_without_the_marker(self, monkeypatch):
        """現行ピン留めの 2022-06-28 版が marker を返すかは未確認なので件数でも見る。"""
        from jp_stock_pipeline.notion.client import QUERY_RESULT_LIMIT, QueryTruncatedError

        client = self._client(monkeypatch, {
            "results": [{"id": str(i)} for i in range(QUERY_RESULT_LIMIT)],
            "has_more": False,
        })
        with pytest.raises(QueryTruncatedError):
            client.query_database("db", page_size=QUERY_RESULT_LIMIT, strict=True)

    def test_non_strict_returns_results_but_warns(self, monkeypatch, caplog):
        client = self._client(monkeypatch, {
            "results": [{"id": "x"}],
            "has_more": False,
            "request_status": {"type": "incomplete"},
        })
        with caplog.at_level("WARNING"):
            rows = client.query_database("db")
        assert len(rows) == 1
        assert any("打ち切られた" in r.message for r in caplog.records)

    def test_normal_result_neither_warns_nor_raises(self, monkeypatch, caplog):
        client = self._client(monkeypatch, {"results": [{"id": "x"}], "has_more": False})
        with caplog.at_level("WARNING"):
            rows = client.query_database("db", strict=True)
        assert len(rows) == 1
        assert not [r for r in caplog.records if "打ち切られた" in r.message]


def test_list_page_property_items_follows_cursor(client):
    """relation が 25 件を超えるページは、プロパティ取得 API を next_cursor で最後まで読む。"""
    first = {"object": "list", "has_more": True, "next_cursor": "c2",
             "results": [{"object": "property_item", "type": "relation", "relation": {"id": "a"}}]}
    second = {"object": "list", "has_more": False, "next_cursor": None,
              "results": [{"object": "property_item", "type": "relation", "relation": {"id": "b"}}]}
    calls = sdk_responses(client, [(200, first, {}), (200, second, {})])
    items = client.list_page_property_items("page-1", "prop%3A1")
    assert [i["relation"]["id"] for i in items] == ["a", "b"]
    assert len(calls) == 2
    assert calls[0].method == "GET" and "/pages/page-1/properties/" in str(calls[0].url)
    assert "start_cursor=c2" in str(calls[1].url)
