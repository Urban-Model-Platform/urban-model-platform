# Result storage adapter package.
#
# Sub-modules:
#   atomic_fs        — atomic filesystem writes (temp-file + os.replace)
#   gpkg_writer      — GeoJSON / FlatGeobuf → GeoPackage conversion
#   ldproxy_entities — build ldproxy provider + service YAML dicts  (V-4)
#   entity_config_backend  — EntityConfigBackendPort ABC + factory    (V-5a)
#   entity_config_fs       — FilesystemEntityConfigBackend            (V-5a)
#   entity_config_k8s      — K8sConfigMapEntityConfigBackend          (V-5b)
#   service_registry       — shared service entity read-modify-write  (V-5c)
#   ldproxy_adapter        — LdproxyResultStorage(ResultStoragePort)  (V-6)
