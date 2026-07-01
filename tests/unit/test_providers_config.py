import pytest

from ump.api.models.providers_config import ProcessConfig


def test_process_config_rejects_legacy_transmission_mode_key():
    with pytest.raises(ValueError, match="transmission-mode-policy"):
        ProcessConfig.model_validate(
            {
                "result-storage": "geoserver",
                "anonymous-access": True,
                "transmission-mode-policy": "emulate-ref",
                "transmissionMode": "value",
            }
        )
