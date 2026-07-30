import re

from ump.core.interfaces.process_id_validator import ProcessIdValidatorPort


class ProcessIdValidator(ProcessIdValidatorPort):
    """Process ID validator using a configurable separator between provider and process.

    The separator is set once at construction and determines the external
    representation UMP uses in its API and stores in the database.
    Default: ":" (e.g. ``fair2adapt:pluvial-flood-risk``).

    Operators can choose a separator that is more URL-friendly (e.g. "-") by
    setting ``UMP_PROCESS_ID_SEPARATOR``.  Changing the separator on an existing
    deployment requires updating existing ``jobs.process_id`` values (migration).
    """

    def __init__(self, separator: str = ":") -> None:
        self._separator = separator
        escaped = re.escape(separator)
        self._pattern = re.compile(rf"([^{escaped}]+){escaped}(.*)")

    def validate(self, process_id_with_prefix: str) -> bool:
        return bool(self._pattern.match(process_id_with_prefix))

    def extract(self, process_id_with_prefix: str) -> tuple[str, str]:
        match = self._pattern.match(process_id_with_prefix)
        if not match:
            raise ValueError(
                f"Process ID {process_id_with_prefix!r} does not match "
                f"pattern 'provider{self._separator}process_id'."
            )
        return match.group(1), match.group(2)

    def create(self, provider_prefix: str, process_id: str) -> str:
        # If the remote server already uses the same separator in its own IDs,
        # extract the bare part to avoid double-prefixing.
        try:
            _, bare = self.extract(process_id)
            process_id = bare
        except ValueError:
            pass
        return f"{provider_prefix}{self._separator}{process_id}"


# Backward-compatible alias kept for any code that imported the old name.
ColonProcessId = ProcessIdValidator
