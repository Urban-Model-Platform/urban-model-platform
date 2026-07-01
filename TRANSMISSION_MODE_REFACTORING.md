# Transmission Mode Policy Refactoring

## Summary

The transmission mode handling has been refactored from a static provider configuration field (`transmissionMode` in `providers.yaml`) to a dynamic, policy-driven execution model.

### Key Changes

1. **Removed legacy field**: `transmissionMode` is no longer accepted in `providers.yaml` (ProcessConfig)
2. **Added transmission-mode-policy**: Controls how UMP handles `transmissionMode` from the execute request
3. **Execution-body-driven**: `transmissionMode` is now read exclusively from the OGC execute request
4. **Policy matrix**: Supports `pass-through`, `emulate-ref`, `emulate-ref-only`, `value-only`

### Architecture

```
Execute Request (transmissionMode) + Provider Config (transmission-mode-policy, result-storage)
       ↓
   Policy Resolver (src/ump/api/transmission_policy.py)
       ↓
   TransmissionDecision (forwarded_mode, delivered_mode, store_results)
       ↓
   Request Rewrite → Remote Server → Result Storage/Delivery
```

## Migration Guide

### 1. Update providers.yaml

**Before:**
```yaml
processes:
  my-process:
    result-storage: "geoserver"
    transmissionMode: "reference"
```

**After:**
```yaml
processes:
  my-process:
    result-storage: "geoserver"
    transmission-mode-policy: "emulate-ref-only"
```

### 2. Policy Selection

Choose the appropriate policy for each process:

- `pass-through`: Remote server behavior unchanged (default)
- `value-only`: Only allow inline results (strict)
- `emulate-ref`: Allow both inline and reference (store optional)
- `emulate-ref-only`: Force reference results via store (strict)

### 3. Client Behavior (No Changes)

Clients continue to submit execute requests with `outputs.*.transmissionMode`:
```json
{
  "inputs": {...},
  "outputs": {
    "result": {"transmissionMode": "reference"}
  }
}
```

### 4. Validation

- All process outputs must use the same `transmissionMode`
- Mixed modes (e.g., `outputA=value`, `outputB=reference`) will return HTTP 400
- Invalid policies or storage combinations will fail at startup

## Production Readiness

✅ Centralized policy decision logic (src/ump/api/transmission_policy.py)
✅ Explicit execution-body extraction (no config fallback)
✅ Early validation on startup (legacy field rejection)
✅ Comprehensive unit tests (12 tests)
✅ Backward compatibility check (test_providers_config.py)
✅ Updated example configuration (providers.yaml.example)
