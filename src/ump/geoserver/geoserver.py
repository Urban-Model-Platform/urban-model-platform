import json
import logging
import os
import shutil
import time
from urllib.parse import quote, urlencode

import requests
from sqlalchemy import text

from ump.api.db_handler import geoserver_engine as engine
from ump.config import app_settings as config
from ump.errors import GeoserverException


class Geoserver:
    RESULTS_FILENAME = "results.geojson"
    RESULTS_TABLE_NAME = "job_results"

    def __init__(self):
        self.workspace = config.UMP_GEOSERVER_WORKSPACE_NAME
        self.errors = []
        self.path_to_results = None
        self.job_id = None
        self._ensure_results_table_exists()

    def _ensure_results_table_exists(self):
        """
        Ensure the central job_results table exists in the geoserver database.
        Creates the table if it doesn't exist.
        """
        try:
            with engine.connect() as conn:
                # Check if table exists
                result = conn.execute(
                    text("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_schema = 'public' 
                        AND table_name = :table_name
                    );
                """),
                    {"table_name": self.RESULTS_TABLE_NAME},
                )

                table_exists = result.scalar()

                if not table_exists:
                    # Create the table with proper PostGIS geometry column
                    conn.execute(
                        text(f"""
                        CREATE TABLE {self.RESULTS_TABLE_NAME} (
                            id SERIAL PRIMARY KEY,
                            job_id VARCHAR(255) NOT NULL,
                            feature_index INTEGER NOT NULL,
                            properties JSONB,
                            geometry GEOMETRY(GEOMETRY, 4326),
                            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                        );
                        
                        CREATE INDEX ON {self.RESULTS_TABLE_NAME} (job_id);
                        CREATE INDEX ON {self.RESULTS_TABLE_NAME} USING GIST (geometry);
                        CREATE UNIQUE INDEX ON {self.RESULTS_TABLE_NAME} (
                            job_id,
                            feature_index
                        );
                    """)
                    )
                    conn.commit()
                    logging.info(
                        f"Created central results table '{self.RESULTS_TABLE_NAME}'"
                    )
                else:
                    logging.debug(
                        f"Results table '{self.RESULTS_TABLE_NAME}' already exists"
                    )

        except Exception as e:
            logging.error(f"Failed to ensure results table exists: {e}")
            raise

    def check_datastore_connection(self):
        """
        Check if Geoserver can establish a connection to its database through datastore.
        This method tests the actual datastore connectivity that Geoserver would use.

        Returns:
            dict: Connection status, response time, and error details.
        """
        try:
            start_time = time.time()

            # Ensure workspace exists
            self.create_workspace()

            # Create a test datastore to verify database connectivity
            test_store_name = "health_check_test_store"
            xml_body = f"""
                <dataStore>
                <name>{test_store_name}</name>
                <connectionParameters>
                    <host>{config.UMP_GEOSERVER_INTERNAL_DB_HOST}</host>
                    <port>{config.UMP_GEOSERVER_INTERNAL_DB_PORT}</port>
                    <database>{config.UMP_GEOSERVER_DB_NAME}</database>
                    <user>{config.UMP_GEOSERVER_DB_USER}</user>
                    <passwd>{config.UMP_GEOSERVER_DB_PASSWORD.get_secret_value()}</passwd>
                    <dbtype>postgis</dbtype>
                </connectionParameters>
                </dataStore>
            """

            # Try to create the test datastore
            response = requests.post(
                f"{config.UMP_GEOSERVER_URL_WORKSPACE}/{self.workspace}/datastores",
                auth=(
                    config.UMP_GEOSERVER_USER,
                    config.UMP_GEOSERVER_PASSWORD.get_secret_value(),
                ),
                data=xml_body,
                headers={"Content-type": "application/xml"},
                timeout=config.UMP_GEOSERVER_CONNECTION_TIMEOUT,
            )

            # Clean up the test datastore immediately
            cleanup_response = requests.delete(
                f"{config.UMP_GEOSERVER_URL_WORKSPACE}/{self.workspace}/datastores/{test_store_name}?recurse=true",
                auth=(
                    config.UMP_GEOSERVER_USER,
                    config.UMP_GEOSERVER_PASSWORD.get_secret_value(),
                ),
                timeout=config.UMP_GEOSERVER_CONNECTION_TIMEOUT,
            )

            response_time = round((time.time() - start_time) * 1000, 2)

            if response.ok:
                return {
                    "status": "healthy",
                    "response_time_ms": response_time,
                    "datastore_test": "success",
                }
            else:
                return {
                    "status": "unhealthy",
                    "error": (
                        f"Datastore creation failed: HTTP {response.status_code} "
                        f"- {response.reason}"
                    ),
                    "response_time_ms": response_time,
                }

        except Exception as e:
            response_time = (
                round((time.time() - start_time) * 1000, 2)
                if "start_time" in locals()
                else None
            )
            logging.error(f"Geoserver datastore connection check failed: {e}")
            return {
                "status": "unhealthy",
                "error": str(e),
                "response_time_ms": response_time,
            }

    def create_workspace(self):
        url = (
            f"{config.UMP_GEOSERVER_URL_WORKSPACE}/{self.workspace}"
            ".json?quietOnNotFound=True"
        )

        response = requests.get(
            url,
            auth=(
                config.UMP_GEOSERVER_USER,
                config.UMP_GEOSERVER_PASSWORD.get_secret_value(),
            ),
            headers={"Content-type": "application/json", "Accept": "application/json"},
            timeout=60,
        )

        if response.status_code == 200:
            logging.info(" --> Workspace %s already exists.", self.workspace)
            return True

        if response.status_code == 404:
            logging.info(" --> Workspace %s not found - creating....", self.workspace)
        else:
            raise GeoserverException(
                f"Geoserver workspace {self.workspace} was not found"
            )

        response = requests.post(
            str(config.UMP_GEOSERVER_URL_WORKSPACE),
            auth=(
                config.UMP_GEOSERVER_USER,
                config.UMP_GEOSERVER_PASSWORD.get_secret_value(),
            ),
            data=f"<workspace><name>{self.workspace}</name></workspace>",
            headers={"Content-type": "text/xml", "Accept": "*/*"},
            timeout=config.UMP_GEOSERVER_CONNECTION_TIMEOUT,
        )

        if response.ok:
            logging.info(" --> Created new workspace %s.", self.workspace)
        else:
            raise GeoserverException("Workspace could not be created")

    def save_results(self, job_id: str, data: dict):
        self.job_id = job_id

        try:
            self.create_workspace()
            logging.info(f"Workspace {self.workspace} created now")

            # Store results in central table
            self.geojson_to_postgis(job_id=job_id, data=data)

            # Ensure central datastore exists
            success = self.create_central_store()

            # Create or update job-specific layer via SQL view
            success = self.create_job_layer(job_id=job_id)

        except Exception as e:
            raise GeoserverException(
                "Result could not be uploaded to the geoserver.",
                payload={"error": type(e).__name__, "message": e},
            ) from e
        return success

    def publish_layer(self, store_name: str, layer_name: str):
        try:
            response = requests.post(
                (
                    f"{config.UMP_GEOSERVER_URL_WORKSPACE}/{self.workspace}"
                    f"/datastores/{store_name}/featuretypes"
                ),
                auth=(
                    config.UMP_GEOSERVER_USER,
                    config.UMP_GEOSERVER_PASSWORD.get_secret_value(),
                ),
                data=f"<featureType><name>{layer_name}</name></featureType>",
                headers={"Content-type": "text/xml"},
                timeout=config.UMP_GEOSERVER_CONNECTION_TIMEOUT,
            )

            if not response or not response.ok:
                logging.error(
                    "Could not publish layer %s from store %s. Reason: %s",
                    layer_name,
                    store_name,
                    response,
                )

        except Exception as e:
            raise GeoserverException(
                (
                    f"Could not publish layer {layer_name} from store "
                    f"{store_name}. Reason: {e}"
                ),
                payload={
                    "error": type(e).__name__,
                    "message": e,
                },
            ) from e

        return response.ok

    # TODO: to simplify the dev setup the UMP and geoserver database hosts
    # can be the same but in production they should be different, at least
    # the database used.
    # Also the user should decide if they want to use the same database
    # (host) for UMP and Geoserver.

    def create_central_store(self):
        """
        Create or update the central datastore that connects to the job_results table.
        This datastore is reused for all job results.
        """
        store_name = "central_results_store"
        logging.info(f" --> Creating/updating central geoserver store {store_name}")

        xml_body = f"""
            <dataStore>
            <name>{store_name}</name>
            <connectionParameters>
                <host>{config.UMP_GEOSERVER_INTERNAL_DB_HOST}</host>
                <port>{config.UMP_GEOSERVER_INTERNAL_DB_PORT}</port>
                <database>{config.UMP_GEOSERVER_DB_NAME}</database>
                <user>{config.UMP_GEOSERVER_DB_USER}</user>
                <passwd>{config.UMP_GEOSERVER_DB_PASSWORD.get_secret_value()}</passwd>
                <dbtype>postgis</dbtype>
            </connectionParameters>
            </dataStore>
        """

        # Try to create the store (will fail if it already exists)
        response = requests.post(
            f"{config.UMP_GEOSERVER_URL_WORKSPACE}/{self.workspace}/datastores",
            auth=(
                config.UMP_GEOSERVER_USER,
                config.UMP_GEOSERVER_PASSWORD.get_secret_value(),
            ),
            data=xml_body,
            headers={"Content-type": "application/xml"},
            timeout=config.UMP_GEOSERVER_CONNECTION_TIMEOUT,
        )

        if response.ok:
            logging.info(f" --> Created central datastore {store_name}")
            return True
        elif response.status_code == 409:  # Conflict - already exists
            logging.info(f" --> Central datastore {store_name} already exists")
            return True
        elif response.status_code == 500:
            # Check if the error message indicates the store already exists
            error_text = response.text.lower() if response.text else ""
            if "already exists" in error_text or "already_exists" in error_text:
                logging.info(
                    (
                        f" --> Central datastore {store_name} already exists "
                        "(detected from 500 error)"
                    )
                )
                return True
            else:
                raise GeoserverException(
                    f"Could not create central datastore {store_name}",
                    payload={
                        "status_code": response.status_code,
                        "message": response.reason,
                        "response_text": response.text[:200] if response.text else None,
                    },
                )
        else:
            raise GeoserverException(
                f"Could not create central datastore {store_name}",
                payload={
                    "status_code": response.status_code,
                    "message": response.reason,
                },
            )

    def create_job_layer(self, job_id: str):
        """
        Create a job-specific layer using a SQL view that filters the
        central table by job_id.
        """
        store_name = "central_results_store"
        layer_name = f"job_{job_id}"

        logging.info(f" --> Creating job-specific layer {layer_name}")

        # Create a SQL view that filters by job_id
        sql_view = f"""
            SELECT 
                id,
                job_id,
                feature_index,
                properties,
                geometry
            FROM {self.RESULTS_TABLE_NAME} 
            WHERE job_id = '{job_id}'
        """

        xml_body = f"""
            <featureType>
                <name>{layer_name}</name>
                <nativeName>{layer_name}</nativeName>
                <title>Results for Job {job_id}</title>
                <abstract>Spatial results for job {job_id}</abstract>
                <keywords>
                    <string>job</string>
                    <string>results</string>
                    <string>{job_id}</string>
                </keywords>
                <srs>EPSG:4326</srs>
                <nativeBoundingBox>
                    <minx>-180.0</minx>
                    <maxx>180.0</maxx>
                    <miny>-90.0</miny>
                    <maxy>90.0</maxy>
                    <crs>EPSG:4326</crs>
                </nativeBoundingBox>
                <metadata>
                    <entry key="JDBC_VIRTUAL_TABLE">
                        <virtualTable>
                            <name>{layer_name}</name>
                            <sql>{sql_view}</sql>
                            <keyColumn>id</keyColumn>
                            <geometry>
                                <name>geometry</name>
                                <type>Geometry</type>
                                <srid>4326</srid>
                            </geometry>
                        </virtualTable>
                    </entry>
                </metadata>
            </featureType>
        """

        response = requests.post(
            f"{config.UMP_GEOSERVER_URL_WORKSPACE}/{self.workspace}/datastores/{store_name}/featuretypes",
            auth=(
                config.UMP_GEOSERVER_USER,
                config.UMP_GEOSERVER_PASSWORD.get_secret_value(),
            ),
            data=xml_body,
            headers={"Content-type": "application/xml"},
            timeout=config.UMP_GEOSERVER_CONNECTION_TIMEOUT,
        )

        if response.ok:
            logging.info(f" --> Created job layer {layer_name}")
            return True
        elif response.status_code == 409:  # Conflict - already exists, try to update
            logging.info(f" --> Layer {layer_name} already exists, updating...")
            # Update existing layer
            update_response = requests.put(
                f"{config.UMP_GEOSERVER_URL_WORKSPACE}/{self.workspace}/datastores/{store_name}/featuretypes/{layer_name}",
                auth=(
                    config.UMP_GEOSERVER_USER,
                    config.UMP_GEOSERVER_PASSWORD.get_secret_value(),
                ),
                data=xml_body,
                headers={"Content-type": "application/xml"},
                timeout=config.UMP_GEOSERVER_CONNECTION_TIMEOUT,
            )
            if update_response.ok:
                logging.info(f" --> Updated job layer {layer_name}")
                return True
            else:
                raise GeoserverException(
                    f"Could not update job layer {layer_name}",
                    payload={
                        "status_code": update_response.status_code,
                        "message": update_response.reason,
                    },
                )
        else:
            raise GeoserverException(
                f"Could not create job layer {layer_name}",
                payload={
                    "status_code": response.status_code,
                    "message": response.reason,
                },
            )

    def geojson_to_postgis(self, job_id: str, data: dict):
        """
        Store GeoJSON data in the central job_results table.
        Each feature is stored as a separate row with job_id reference.
        """
        try:
            # Validate GeoJSON structure
            if "features" not in data:
                raise ValueError("Invalid GeoJSON: 'features' key not found")

            features = data["features"]
            if not features:
                raise ValueError("No features found in GeoJSON data")

            # Delete existing results for this job_id
            with engine.connect() as conn:
                conn.execute(
                    text(
                        f"DELETE FROM {self.RESULTS_TABLE_NAME} WHERE job_id = :job_id"
                    ),
                    {"job_id": job_id},
                )

                # Insert new features
                for feature_index, feature in enumerate(features):
                    geometry = feature.get("geometry")
                    properties = feature.get("properties", {})

                    if geometry:
                        # Convert geometry to WKT for PostGIS using shapely
                        from shapely.geometry import shape

                        shapely_geom = shape(geometry)
                        wkt_geometry = shapely_geom.wkt

                        conn.execute(
                            text(f"""
                            INSERT INTO {self.RESULTS_TABLE_NAME} 
                            (
                                job_id,
                                feature_index,
                                properties,
                                geometry,
                                created_at,
                                updated_at
                            )
                            VALUES (
                                :job_id, 
                                :feature_index, 
                                :properties, 
                                ST_GeomFromText(:geometry, 4326),
                                NOW(),
                                NOW()
                            )
                        """),
                            {
                                "job_id": job_id,
                                "feature_index": feature_index,
                                "properties": json.dumps(properties),
                                "geometry": wkt_geometry,
                            },
                        )

                conn.commit()

            logging.info(
                (
                    f"Successfully saved {len(features)} features for job "
                    f"'{job_id}' to central results table"
                )
            )

        except Exception as e:
            logging.error(
                (
                    f"Failed to save GeoJSON for job '{job_id}' to central "
                    f"results table: {e}"
                )
            )
            raise

    def delete_job_results(self, job_id: str):
        """
        Delete results for a specific job from the central table and remove
        the Geoserver layer.
        """
        try:
            # Delete layer from Geoserver
            layer_name = f"job_{job_id}"
            store_name = "central_results_store"

            response = requests.delete(
                f"{config.UMP_GEOSERVER_URL_WORKSPACE}/{self.workspace}/datastores/{store_name}/featuretypes/{layer_name}",
                auth=(
                    config.UMP_GEOSERVER_USER,
                    config.UMP_GEOSERVER_PASSWORD.get_secret_value(),
                ),
                timeout=config.UMP_GEOSERVER_CONNECTION_TIMEOUT,
            )

            if (
                response.ok or response.status_code == 404
            ):  # OK or not found (already deleted)
                logging.info(f"Deleted Geoserver layer for job '{job_id}'")
            else:
                logging.warning(
                    (
                        f"Failed to delete Geoserver layer for job '{job_id}': "
                        f"{response.status_code}"
                    )
                )

            # Delete data from central table
            with engine.connect() as conn:
                result = conn.execute(
                    text(
                        f"DELETE FROM {self.RESULTS_TABLE_NAME} WHERE job_id = :job_id"
                    ),
                    {"job_id": job_id},
                )
                deleted_rows = result.rowcount
                conn.commit()

            logging.info(
                (
                    f"Deleted {deleted_rows} result rows for job '{job_id}' "
                    "from central table"
                )
            )

        except Exception as e:
            logging.error(f"Failed to delete results for job '{job_id}': {e}")
            raise

    def get_layer_wfs_url(self, job_id: str) -> str:
        """Return the WFS GetFeature URL for a job's GeoServer layer.

        Used by the OGC API Processes reference transmission mode to
        deliver result links per OGC API Processes - Part 1: Core
        (Clause 7.13) and RFC 8288 Web Linking.

        Args:
            job_id: Job identifier whose result layer should be linked.

        Returns:
            Fully qualified WFS GetFeature URL returning GeoJSON.
        """
        layer_name = f"job_{job_id}"
        type_name = f"{self.workspace}:{layer_name}"

        query = urlencode(
            {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeName": type_name,
                "outputFormat": "application/json",
            }
        )
        geoserver_base_url = config.UMP_GEOSERVER_PUBLIC_URL or config.UMP_GEOSERVER_URL
        wfs_url = f"{geoserver_base_url}/wfs?{query}"

        logging.debug(
            "Built WFS reference URL for job '%s' layer '%s': %s",
            job_id,
            type_name,
            wfs_url,
        )
        return wfs_url

    def get_layer_oaf_url(self, job_id: str) -> str:
        """Return the OAF items URL for a job's GeoServer layer.

        Args:
            job_id: Job identifier whose result layer should be linked.

        Returns:
            Fully qualified OGC API Features items URL returning GeoJSON.
        """
        layer_name = f"job_{job_id}"
        collection_id = f"{self.workspace}:{layer_name}"
        encoded_collection_id = quote(collection_id, safe="")

        query = urlencode({"f": "json"})
        geoserver_base_url = config.UMP_GEOSERVER_PUBLIC_URL or config.UMP_GEOSERVER_URL
        oaf_url = (
            f"{geoserver_base_url}/ogc/features/v1/collections/"
            f"{encoded_collection_id}/items?{query}"
        )

        logging.debug(
            "Built OAF reference URL for job '%s' collection '%s': %s",
            job_id,
            collection_id,
            oaf_url,
        )
        return oaf_url

    def get_layer_reference_url(self, job_id: str) -> str:
        """Return a reference URL for a job layer based on configured protocol."""
        if config.UMP_GEOSERVER_SERVICE_TYPE == "oaf":
            return self.get_layer_oaf_url(job_id)
        return self.get_layer_wfs_url(job_id)

    def cleanup(self):
        if self.path_to_results and os.path.exists(self.path_to_results):
            shutil.rmtree(self.path_to_results)
