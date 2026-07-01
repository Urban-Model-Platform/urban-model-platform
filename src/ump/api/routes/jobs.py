import asyncio
import json
import logging
from datetime import datetime, timezone

from apiflask import APIBlueprint
from flask import Response, g, request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from ump.api.models.ensemble import JobsUsers
from ump.api.models.job import Job
from ump.api.models.job_comments import JobComment
from ump.api.jobs import append_ensemble_list, get_jobs
from ump.api.keycloak_utils import find_user_id_by_email
from ump.api.db_handler import engine

jobs = APIBlueprint("jobs", __name__)

@jobs.route("/", defaults={"page": "index"})
def index(page):
    args = request.args.to_dict(flat=False) if request.args else {}
    result = get_jobs(args, g.get("auth_token"))
    if "include_ensembles" in args and args["include_ensembles"]:
        for job in result["jobs"]:
            append_ensemble_list(job)
    return Response(json.dumps(result), mimetype="application/json")


@jobs.route("/<path:job_id>/results", methods=["GET"])
def get_results(job_id=None):
    """Return job results with OGC API Processes transmission mode support.

    OGC API Processes - Part 1: Core (Clause 7.13).
    See: https://docs.ogc.org/is/18-062r2/18-062r2.html#toc34

    Response semantics:

    - ``transmission_mode='value'`` (default):
      200 OK with ``application/json`` body containing the inline results
      (per ``results.yaml`` / ``inlineOrRefData.yaml``).
    - ``transmission_mode='reference'``:
      200 OK with ``application/json`` body where each output is a
      ``link.yaml`` object pointing to the result resource (e.g. a
      GeoServer WFS layer). An RFC 8288 ``Link`` header is added for
      each referenced output to support HTTP-level link consumers.

    Args:
        job_id: The job identifier.

    Returns:
        flask.Response: OGC-compliant results document, optionally
        accompanied by RFC 8288 ``Link`` headers.
    """
    auth = g.get("auth_token")
    job = Job(job_id, None if auth is None else auth["sub"])

    results = asyncio.run(job.results())

    response = Response(
        json.dumps(results),
        mimetype="application/json",
        status=200,
    )

    if job.transmission_mode == "reference":
        _add_reference_link_headers(response, results, job_id)

    return response


def _add_reference_link_headers(response, results, job_id):
    """Attach RFC 8288 ``Link`` headers for OGC reference outputs.

    Each output value in ``results`` is expected to follow the OGC
    ``link.yaml`` schema with at least an ``href`` field. Outputs that
    are not link-shaped (i.e. inline values) are skipped silently.
    """
    if not isinstance(results, dict):
        return

    link_values = []
    for output_id, output in results.items():
        if not isinstance(output, dict):
            continue
        href = output.get("href")
        if not href:
            continue

        media_type = output.get("type", "application/json")
        rel = output.get(
            "rel",
            "http://www.opengis.net/def/rel/ogc/1.0/results",
        )
        title = output.get("title", f"Result '{output_id}'")
        link_values.append(
            f'<{href}>; rel="{rel}"; type="{media_type}"; title="{title}"'
        )

    if not link_values:
        return

    response.headers["Link"] = ", ".join(link_values)
    logging.debug(
        "Job %s: added %d RFC 8288 Link header(s) for reference outputs",
        job_id,
        len(link_values),
    )


@jobs.route("/<path:job_id>/users", methods=["GET"])
def get_users(job_id=None):
    """Get all users that have access to a job"""
    auth = g.get("auth_token")
    if auth is None:
        return Response("[]", mimetype="application/json")
    with Session(engine) as session:
        stmt = select(JobsUsers).where(JobsUsers.job_id == job_id)
        list = []
        for user in session.scalars(stmt).fetchall():
            list.append(user.to_dict())
        return list


@jobs.route("/<path:job_id>/share/<path:email>", methods=["GET"])
def share(job_id=None, email=None):
    """Share a job with another user"""
    auth = g.get("auth_token")
    user_id = find_user_id_by_email(email)
    if user_id is None:
        logging.error("Unable to find user by email %s.", email)
        return Response(status=404)
    if auth is None:
        logging.error("Authentication token is missing.")
        return Response(status=401)

    own_user_id = auth["sub"]

    job = Job(job_id, None if auth is None else own_user_id)
    if job is None:
        logging.error("Unable to find job with id %s.", job_id)
        return Response(status=404)

    with Session(engine) as session:
        own_entry = JobsUsers(job_id=job_id, user_id=own_user_id)
        session.add(own_entry)

        shared_entry = JobsUsers(job_id=job_id, user_id=user_id)
        session.add(shared_entry)

        session.commit()
        return Response(status=201)


@jobs.route("/<path:job_id>/comments", methods=["GET"])
def get_comments(job_id):
    """Get all comments for a job"""
    auth = g.get("auth_token")
    if auth is None:
        return Response("[]", mimetype="application/json")
    with Session(engine) as session:
        stmt = (
            select(JobComment)
            .distinct()
            .join(JobsUsers, JobsUsers.job_id == JobComment.job_id, isouter=True)
            .where(
                or_(JobComment.user_id == auth["sub"], JobsUsers.user_id == auth["sub"])
            )
            .where(JobComment.job_id == job_id)
        )
        results = []
        for comment in session.scalars(stmt).fetchall():
            results.append(comment.to_dict())
        return results


@jobs.route("/<path:job_id>/comments", methods=["POST"])
def create_comment(job_id):
    """Create a comment for a job"""
    auth = g.get("auth_token")
    if auth is None:
        logging.error("Not creating comment, no authentication found.")
        return Response(
            '{"error_message": "not authenticated"}',
            mimetype="application/json",
            status=401,
        )
    comment = JobComment(
        user_id=auth["sub"],
        job_id=job_id,
        comment=request.get_json()["comment"],
        created=datetime.now(timezone.utc),
        modified=datetime.now(timezone.utc),
    )
    with Session(engine) as session:
        session.add(comment)
        session.commit()
        return Response(
            json.dumps(comment.to_dict()), mimetype="application/json", status=201
        )


@jobs.route("/<path:job_id>", methods=["GET"])
def show(job_id=None):
    auth = g.get("auth_token")
    if request.args.get("additionalMetadata") == "true":
        job = Job(job_id, None if auth is None else auth["sub"]).display(additional_metadata=True)
    else:
        job = Job(job_id, None if auth is None else auth["sub"]).display()
    append_ensemble_list(job)
    return Response(json.dumps(job), mimetype="application/json")
