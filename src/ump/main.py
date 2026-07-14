# TODO: this file has become a hodgepodge of very different things,
# it should be split into dedicated files:
# - an app factory for migrations
# - logging setup
# - a geoserver cleanup runner
# - the flask app
# - the token verification
import atexit
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from logging.config import dictConfig

import requests
from apiflask import APIBlueprint, APIFlask
from flask import g, jsonify, request
from flask_cors import CORS
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from keycloak import KeycloakConnectionError, KeycloakGetError, KeycloakOpenID
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from ump.api.db_handler import DBHandler, close_pool
from ump.api.providers import PROVIDERS
from ump.api.routes.ensembles import ensembles
from ump.api.routes.health import health_bp
from ump.api.routes.jobs import jobs
from ump.api.routes.processes import processes
from ump.api.routes.users import users
from ump.config import app_settings as config
from ump.errors import CustomException, OGCProcessException

dictConfig(
    {
        "version": 1,
        "formatters": {
            "default": {
                "format": "[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
            }
        },
        "handlers": {
            "wsgi": {
                "class": "logging.StreamHandler",
                "stream": "ext://flask.logging.wsgi_errors_stream",
                "formatter": "default",
            }
        },
        "root": {"level": config.UMP_LOG_LEVEL, "handlers": ["wsgi"]},
        "loggers": {
            "ump.api.providers": {  # Configure the logger for providers.py
                "level": config.UMP_LOG_LEVEL,
                "handlers": ["wsgi"],
                "propagate": False,  # Prevent duplicate logging
            },
            "ump.api.processes": {  # Configure the logger for processes.py
                "level": config.UMP_LOG_LEVEL,
                "handlers": ["wsgi"],
                "propagate": False,
            },
            "ump.api.models.process": {  # Configure the logger for processes.py
                "level": config.UMP_LOG_LEVEL,
                "handlers": ["wsgi"],
                "propagate": False,
            },
        },
    }
)


def cleanup():
    """Deletes jobs of anonymous users older than UMP_JOB_RETENTION_MINUTES."""
    sql = "delete from jobs where user_id is null and finished < %(finished)s returning job_id, provider_prefix, process_id"
    finished = datetime.now() - timedelta(minutes=config.UMP_JOB_RETENTION_MINUTES)

    with DBHandler() as conn:
        result = conn.run_query(sql, query_params={"finished": finished})
        for row in result:
            # get additional job metadata
            job_id, provider_prefix, process_id = row

            # Check if result-storage is set to geoserver
            result_storage = (
                PROVIDERS.get(provider_prefix, {})
                .get("processes", {})
                .get(process_id, {})
                .get("result-storage", None)
            )
            if result_storage == "geoserver":
                from ump.geoserver.geoserver import Geoserver

                geoserver = Geoserver()
                try:
                    geoserver.delete_job_results(job_id)
                except Exception as e:
                    logging.error(
                        f"Failed to cleanup geoserver results for job {job_id}: {e}"
                    )


# Run the cleanup task in a background daemon thread so it fires independently
# of incoming requests (the previous schedule.run_pending()-in-before_request
# approach only triggered when the API was actually being used).
if config.UMP_JOB_CLEANUP_ENABLED:

    def _cleanup_loop():
        logging.info(
            "Job cleanup scheduler started: interval=%ds, retention=%dmin",
            config.UMP_JOB_CLEANUP_INTERVAL_SECONDS,
            config.UMP_JOB_RETENTION_MINUTES,
        )
        while True:
            time.sleep(config.UMP_JOB_CLEANUP_INTERVAL_SECONDS)
            try:
                cleanup()
            except Exception as exc:
                logging.error("Job cleanup failed: %s", exc)

    _cleanup_thread = threading.Thread(
        target=_cleanup_loop, daemon=True, name="job-cleanup"
    )
    _cleanup_thread.start()
else:
    logging.info("Job cleanup disabled (UMP_JOB_CLEANUP_ENABLED=False).")

app = APIFlask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

app.config["DEBUG"] = os.environ.get("FLASK_DEBUG", 0)
app.config["SQLALCHEMY_DATABASE_URI"] = (
    f"postgresql+psycopg2://{config.UMP_DATABASE_USER}:{config.UMP_DATABASE_PASSWORD.get_secret_value()}"
    + f"@{config.UMP_DATABASE_HOST}:{config.UMP_DATABASE_PORT}/{config.UMP_DATABASE_NAME}"
)

db = SQLAlchemy(app)
migrate = Migrate(app, db)

CORS(app)

api = APIBlueprint("api", __name__, url_prefix=config.UMP_API_SERVER_URL_PREFIX)
api.register_blueprint(processes, url_prefix="/processes")
api.register_blueprint(jobs, url_prefix="/jobs")
api.register_blueprint(ensembles, url_prefix="/ensembles")
api.register_blueprint(users, url_prefix="/users")
api.register_blueprint(health_bp, url_prefix="/health")

app.register_blueprint(api)

# this does not check the connection yet, so app can fail later on!

keycloak_openid = KeycloakOpenID(
    server_url=str(config.UMP_KEYCLOAK_URL),
    client_id=config.UMP_KEYCLOAK_CLIENT_ID,
    realm_name=config.UMP_KEYCLOAK_REALM,
)


@app.errorhandler(OGCProcessException)
def handle_ogc_exception(error: OGCProcessException):
    response = jsonify(error.response.model_dump(exclude_unset=True))
    response.status_code = error.response.status
    response.content_type = "application/problem+json"

    if response.status_code in (401, 403):
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


@app.before_request
def check_jwt():
    """Decodes the JWT token and stores user info in flask g"""
    auth = request.authorization

    # TODO: this needs to be improved, it should not fail the app
    # and error messages must adopt OGCProcessException
    if auth is not None:
        # need exception handling here to avoid app failure!
        try:
            # TODO: generally token verification is done offline,
            # but decode_token connects to keycloak on every request (for retrieving keys)
            # use a library like PyJWT or better Authlib
            decoded = keycloak_openid.decode_token(auth.token)
        except KeycloakGetError as e:
            app.logger.error(e)
            raise CustomException(
                message="Keycloak: Resource not found. Check Keycloak URL path.",
                status_code=404,
            )
        except KeycloakConnectionError as e:
            app.logger.error(e)
            raise CustomException(
                message="Keycloak: Connection error. Check Keycloak URL host.",
                status_code=500,
            )
        except Exception as e:
            app.logger.error(e)
            raise CustomException(
                message="Keycloak: Unknown error. Check Keycloak URL.",
                status_code=500,
            )
        g.auth_token = decoded
    else:
        g.auth_token = None
    pass


@app.after_request
def set_headers(response):
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.errorhandler(CustomException)
def handle_invalid_usage(error):
    response = jsonify(error.to_dict())
    response.status_code = error.status_code
    return response


@app.errorhandler(HTTPException)
def handle_http_exception(error):
    """Return JSON instead of HTML for HTTP errors."""
    # start with the correct headers and status code from the error
    response = error.get_response()
    # replace the body with JSON
    response.data = json.dumps(
        {
            "code": error.code,
            "name": error.name,
            "description": error.description,
        }
    )
    response.content_type = "application/json"
    return response


@atexit.register
def shutdown_pool_on_exit():
    """Close the connection pool when the application shuts down."""
    close_pool()


if __name__ == "__main__":
    app.run(host="0.0.0.0")
