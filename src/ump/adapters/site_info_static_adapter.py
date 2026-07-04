from ump.core.interfaces.site_info import SiteInfoPort
from ump.core.settings import app_settings


class StaticSiteInfoAdapter(SiteInfoPort):
    def get_site_info(self):
        base = app_settings.UMP_API_SERVER_URL_PREFIX.rstrip("/") or ""
        routes = []
        # Provide routes for each supported API version
        for ver in getattr(app_settings, "UMP_SUPPORTED_API_VERSIONS", ["1.0"]):
            prefix = f"{base}/v{ver}"
            routes.append(
                {
                    "path": f"{prefix}/processes",
                    "description": f"List available processes (v{ver})",
                }
            )
            routes.append(
                {
                    "path": f"{prefix}/jobs",
                    "description": f"List submitted jobs (v{ver})",
                }
            )
            # link to OpenAPI definition for the versioned API
            routes.append(
                {
                    "path": f"{prefix}/openapi.json",
                    "description": f"OpenAPI definition (v{ver})",
                }
            )

        return {
            "title": app_settings.UMP_SITE_TITLE,
            "description": app_settings.UMP_SITE_DESCRIPTION,
            "contact": app_settings.UMP_SITE_CONTACT,
            "routes": routes,
        }
