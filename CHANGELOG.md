# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](http://semver.org/spec/v2.0.0.html).
# [3.x]
- complete refactoring 

# [2.x]
## [2.1.0] - 2025-07-31
### Changed:
- improved error handling when requesting remote servers processes and jobs
- helm chart is up-to-date with current UMP
- provider loader listens on any event to be compatible with configmap-updates in k8s
- provider loader improved: debouncing rapid file changes and atomic updates of providers object
- improved server responses in certain situations, especially when something went wrong showing users json information (as this is a json api) in accordance with OGC api process spec
- improved job starting mechanism

### Added:
- a new setting to control gunicorn worker timout was introduced: UMP_SERVER_TIMEOUT
- a new setting to control ump server path prefix introduced: UMP_API_SERVER_URL_PREFIX
- timeouts for all requests to remote servers

### Fixed:
- using setting UMP_KEYCLOAK_CLIENT_ID instead of hard-coded "ump-client"
- job insert queries failed when logged-in user created a job
- missing job metadata
- fetch correct job status from remote server

## [2.0.0] - 2025-06-25

### Added
- comprehensive documentation added
- unified database connection pool handling 

### Changed
- created a providers pydantic class for better type safety and concise handling 
- improved provider.yaml loading and provider updateing mechanism
- improved logging 
- keycloak coinnection error handling improved

### Fixed
- ump ran out of database connections due to unclosed connections

# [1.x]
## [1.2.0] - 2024-05-25

### Added
- documentation
- keycoak connection error handling
- complete database migrations
- a helm chart

### Fixed
- missing keycloak env vars
-

### Changed
- Improved start-dev in Makefile, enhanced database issues
- using pydantic classes in some cases now (e.g. for provider config)
- load processes async
- made the process of determining which are visible to the user more concise and more explicit
- added base logger and logging
- simplyfied keycloak connection settings
- improved settings management
- improved database connection pooling and connection re-use
- fixed dev setup and eased dev setup mileage

## [1.1.0] - 2024-07-22

### Changed

- added package management system (poetry)
- using project template (copier)
- moved source code inside src folder and restructured it
