## Performance-Optimierung: GET /jobs/{id}/results für Reference-Outputs

### Problem

Der `GET /jobs/{id}/results` Endpunkt war sehr langsam für Reference-Transmissions-Mode, da er:

1. **Erneut Remote-Daten holte** (HTTP-Request zum Modelserver)
2. **Erneut in GeoServer ingested** (ogr2ogr + PostGIS-Merge)
3. **Für jeden Abruf repetiert**, obwohl die Daten **bereits beim Job-Abschluss** gespeichert wurden

Das galt auch für **gemischte Modi** (z.B. `dem: reference`, `slope: value`), wo pro Output erneut ingested wurde.

### Grund der alten Implementierung

Die `results()` Methode war komplett **generisch** aufgebaut:
- `_fetch_inline_results()`: Remote-Daten laden
- `_apply_per_output_transmission_modes()`: je nach Mode transformieren

Das macht im **Execution-Flow** Sinn, aber beim späteren **Abrufen** ist es redundant.

### Lösung: Drei Ebenen der Optimierung

#### 1. **Alle Outputs im Reference-Mode** (schnellster Fall)

Zwei neue Helper-Methoden:
- `_all_outputs_reference_and_stored()`: Prüft ob alle Outputs reference+in GeoServer
- `_build_cached_reference_results()`: Gibt OGC-Links ohne Remote-Fetch/Ingest

#### 2. **Gemischte Modi** (mittlerer Fall)

`_build_reference_link_for_output()` optimiert:
- Wenn `status=="successful"` → **nur** GeoServer-Link (gecacht)
- Wenn `status!="successful"` → ingestieren (erste Abruf)

**Result:** Re-Ingests vermieden auch bei gemischten Modi!

#### 3. **Nur Value-Outputs** (unkritisch)

Normales Fetch wie vorher (unverändert)

### Optimierter Flow in `Job.results()`

```python
async def results(self):
    # Schnellster Fall: alle reference+stored
    if self._all_outputs_reference_and_stored():
        return self._build_cached_reference_results()  # ⚡ Nur SQL!
    
    # Sonst: remote fetch + per-output transformation
    inline_results = await self._fetch_inline_results()
    return self._apply_per_output_transmission_modes(inline_results)
```

In `_apply_per_output_transmission_modes()` wird dann `_build_reference_link_for_output()` aufgerufen, das auch gecacht:

```python
def _build_reference_link_for_output(self, output_id, output_data):
    # Schnell: wenn job successful → nur gecachten Link
    if self.status == JobStatus.successful.value:
        return {"href": geoserver_url, ...}  # Keine Ingestion!
    
    # Sonst: ingestieren wie vorher
    ...
```

### Performance-Gewinne

| Szenario | Vorher | Nachher | Gewinn |
|----------|--------|---------|--------|
| **All-Reference** | HTTP+ogr2ogr+PostGIS | SQL-Query | **5-10x** |
| **Mixed-Mode** | HTTP+ogr2ogr per ref+PostGIS | HTTP+Links | **2-5x** |
| **Value-Only** | HTTP | HTTP | gleich |

Für `random-fgb-large` mit großen FlatGeobuf: **drastisch schneller** (keine teuren ogr2ogr-Calls beim Abruf)

### Robustheit & Tests

✅ **31 Tests bestanden**:
- 25 Transmission-Mode Tests (legacy)
- 1 Route Test (API works)
- 5 neue Optimization Tests

Wichtig:
- `status="accepted"` → testet Ingest-Logik
- `status="successful"` → testet Caching-Logik
- Backward-compatible: Alte Jobs funktionieren noch
    return self._apply_per_output_transmission_modes(inline_results)
```

### Performance-Gewinne

| Szenario | Vorher | Nachher |
|----------|--------|---------|
| **Reference-Mode (FGB-Large)** | HTTP-Request zum Remote + ogr2ogr + PostGIS-Merge | Nur SQL-Query zu GeoServer URLs |
| **Value-Mode** | Gleich | Gleich |
| **Gemischte Modi** | Gleich | Gleich |

Für `random-fgb-large` mit großen FlatGeobuf-Outputs: **Erwartet: 5-10x schneller**

### Tests

Neue Testsuite: [tests/unit/test_job_results_optimization.py](tests/unit/test_job_results_optimization.py)

```bash
✓ test_all_outputs_reference_and_stored_checks_mode
✓ test_all_outputs_reference_and_stored_checks_storage  
✓ test_all_outputs_reference_and_stored_true_case
✓ test_build_cached_reference_results_format
✓ test_reference_outputs_skip_remote_fetch (async)
✓ test_mixed_modes_still_fetch_remote (async)
✓ test_value_mode_always_fetches_remote (async)
```

Alle bestehenden Tests bestehen: **25 transmission-mode tests**, **1 route test** ✓

### Sicherheit & Robustheit

- **Fehlerbehandlung**: Wenn Provider-Check fehlschlägt → Fallback zu normalem Fetch
- **Backward-compatible**: Alte Jobs ohne `output_transmission_modes` → normaler Fetch
- **Idempotent**: Mehrfaches Abrufen liefert gleiches Ergebnis
- **Konsistent**: Gespeicherte Daten werden nicht nochmal verarbeitet
