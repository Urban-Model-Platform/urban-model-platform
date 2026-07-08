import json

from apiflask import APIFlask

import ump.api.routes.jobs as jobs_module


class _FakeJobMixedResults:
    def __init__(self, job_id=None, user=None):
        self.job_id = job_id
        self.user = user
        # Global mode is irrelevant for mixed responses; route should inspect payload.
        self.transmission_mode = "value"

    async def results(self):
        return {
            "dem": {
                "href": "http://geoserver:8080/geoserver/wfs?service=WFS&request=GetFeature&typeName=demo:job-1",
                "rel": "http://www.opengis.net/def/rel/ogc/1.0/results",
                "type": "application/geo+json",
                "title": "Result 'dem' for job job-1",
            },
            "slope": {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {"v": 1}, "geometry": None}
                ],
            },
        }


def test_get_results_with_fake_model_mixed_outputs_has_ogc_body_and_link_header(
    monkeypatch,
):
    """Smoke-test /jobs/{id}/results with a fake model returning mixed outputs."""
    monkeypatch.setattr(jobs_module, "Job", _FakeJobMixedResults)

    app = APIFlask(__name__)
    app.register_blueprint(jobs_module.jobs, url_prefix="/jobs")

    with app.test_client() as client:
        response = client.get("/jobs/job-1/results")

    assert response.status_code == 200
    assert response.mimetype == "application/json"

    payload = json.loads(response.data)

    # OGC results document: output ids map to inlineOrRefData values
    assert "dem" in payload
    assert "slope" in payload

    # Reference output is OGC link.yaml-like
    assert payload["dem"]["href"].startswith("http://geoserver")
    assert payload["dem"]["rel"] == "http://www.opengis.net/def/rel/ogc/1.0/results"
    assert payload["dem"]["type"] == "application/geo+json"

    # Value output remains inline
    assert payload["slope"]["type"] == "FeatureCollection"

    # Route should add RFC 8288 Link header for referenced outputs
    link_header = response.headers.get("Link")
    assert link_header is not None
    assert "http://geoserver:8080/geoserver/wfs" in link_header
    assert 'rel="http://www.opengis.net/def/rel/ogc/1.0/results"' in link_header
