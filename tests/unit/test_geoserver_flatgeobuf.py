from unittest.mock import patch

import pytest

from ump.errors import GeoserverException
from ump.geoserver.geoserver import Geoserver


def _make_geoserver_without_db_init(monkeypatch) -> Geoserver:
    monkeypatch.setattr(Geoserver, "_ensure_results_table_exists", lambda self: None)
    return Geoserver()


def test_save_flatgeobuf_results_orchestrates_import_and_publish(monkeypatch):
    geoserver = _make_geoserver_without_db_init(monkeypatch)

    monkeypatch.setattr(geoserver, "create_workspace", lambda: True)
    monkeypatch.setattr(geoserver, "create_central_store", lambda: True)
    monkeypatch.setattr(geoserver, "create_job_layer", lambda job_id: True)

    import_calls = []
    merge_calls = []
    drop_calls = []

    monkeypatch.setattr(
        geoserver,
        "_ogr2ogr_import_flatgeobuf",
        lambda source, temp_table: import_calls.append((source, temp_table)),
    )
    monkeypatch.setattr(
        geoserver,
        "_merge_temp_table_into_results",
        lambda job_id, temp_table: merge_calls.append((job_id, temp_table)),
    )
    monkeypatch.setattr(
        geoserver,
        "_drop_table_if_exists",
        lambda table_name: drop_calls.append(table_name),
    )

    result = geoserver.save_flatgeobuf_results(
        job_id="job-1",
        source="http://remote/results.fgb",
    )

    assert result is True
    assert len(import_calls) == 1
    assert import_calls[0][0] == "http://remote/results.fgb"
    assert len(merge_calls) == 1
    assert merge_calls[0][0] == "job-1"
    assert len(drop_calls) == 1


def test_ogr2ogr_import_handles_missing_binary(monkeypatch):
    geoserver = _make_geoserver_without_db_init(monkeypatch)

    def _raise_file_not_found(*_args, **_kwargs):
        raise FileNotFoundError("ogr2ogr not found")

    with patch(
        "ump.geoserver.geoserver.subprocess.run", side_effect=_raise_file_not_found
    ):
        with pytest.raises(GeoserverException) as exc:
            geoserver._ogr2ogr_import_flatgeobuf("/tmp/in.fgb", "tmp_table")

    assert "ogr2ogr is not installed" in str(exc.value)


def test_ogr2ogr_import_handles_nonzero_exit(monkeypatch):
    geoserver = _make_geoserver_without_db_init(monkeypatch)

    error = __import__("subprocess").CalledProcessError(
        returncode=1,
        cmd="ogr2ogr",
        stderr="import failed",
    )

    with patch("ump.geoserver.geoserver.subprocess.run", side_effect=error):
        with pytest.raises(GeoserverException) as exc:
            geoserver._ogr2ogr_import_flatgeobuf("/tmp/in.fgb", "tmp_table")

    assert "ogr2ogr FlatGeobuf import failed" in str(exc.value)


def test_save_flatgeobuf_bytes_uses_temp_file_path(monkeypatch):
    geoserver = _make_geoserver_without_db_init(monkeypatch)

    captured = {}

    def _fake_save_flatgeobuf_results(job_id, source):
        captured["job_id"] = job_id
        captured["source"] = source
        return True

    monkeypatch.setattr(
        geoserver, "save_flatgeobuf_results", _fake_save_flatgeobuf_results
    )

    result = geoserver.save_flatgeobuf_bytes("job-1", b"flatgeobuf-bytes")

    assert result is True
    assert captured["job_id"] == "job-1"
    assert isinstance(captured["source"], str)
    assert captured["source"].endswith(".fgb")
