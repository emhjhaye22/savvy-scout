from unittest.mock import MagicMock, patch

from savvy_scout.graph.drive import upload_file


def _mock_response(status_code=202, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.raise_for_status.return_value = None
    return resp


def test_upload_file_creates_session_and_uploads_single_chunk(tmp_path):
    source = tmp_path / "savvy_scout_20260917.db"
    source.write_bytes(b"small backup contents")

    session_resp = _mock_response(json_body={"uploadUrl": "https://upload.example/session123"})
    chunk_resp = _mock_response(status_code=201, json_body={"webUrl": "https://onedrive.example/backup.db"})

    with patch("savvy_scout.graph.drive._get_access_token", return_value="fake-token"), \
         patch("savvy_scout.graph.drive.requests.post", return_value=session_resp) as mock_post, \
         patch("savvy_scout.graph.drive.requests.put", return_value=chunk_resp) as mock_put:
        url = upload_file(
            str(source), "TenderSight-Backups", "mark@bidsavvy.io",
            "tenant", "client", "secret",
        )

    assert url == "https://onedrive.example/backup.db"
    mock_post.assert_called_once()
    assert "createUploadSession" in mock_post.call_args[0][0]
    mock_put.assert_called_once()
    put_headers = mock_put.call_args.kwargs["headers"]
    assert put_headers["Content-Range"] == f"bytes 0-{len(b'small backup contents') - 1}/{len(b'small backup contents')}"


def test_upload_file_splits_large_file_into_multiple_chunks(tmp_path):
    from savvy_scout.graph.drive import CHUNK_SIZE

    source = tmp_path / "big_backup.db"
    source.write_bytes(b"x" * (CHUNK_SIZE + 100))  # forces exactly 2 chunks

    session_resp = _mock_response(json_body={"uploadUrl": "https://upload.example/session456"})
    mid_chunk_resp = _mock_response(status_code=202, json_body={})
    final_chunk_resp = _mock_response(status_code=201, json_body={"webUrl": "https://onedrive.example/big.db"})

    with patch("savvy_scout.graph.drive._get_access_token", return_value="fake-token"), \
         patch("savvy_scout.graph.drive.requests.post", return_value=session_resp), \
         patch("savvy_scout.graph.drive.requests.put", side_effect=[mid_chunk_resp, final_chunk_resp]) as mock_put:
        url = upload_file(
            str(source), "TenderSight-Backups", "mark@bidsavvy.io",
            "tenant", "client", "secret",
        )

    assert url == "https://onedrive.example/big.db"
    assert mock_put.call_count == 2
