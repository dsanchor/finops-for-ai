import csv
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Identity:
    department: str
    team: str
    user: str
    subscription_key: str | None = field(default=None, repr=False)

    def headers(self) -> dict[str, str]:
        headers = {
            "X-Department": self.department,
            "X-Team": self.team,
            "X-User": self.user,
        }
        if self.subscription_key is not None:
            headers["Ocp-Apim-Subscription-Key"] = self.subscription_key
        return headers


def load_identities(path: Path) -> list[Identity]:
    identities = []
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, strict=True)
        try:
            for row in reader:
                values = [value.strip() for value in row]
                if len(values) not in (3, 4) or any(not value for value in values[:3]):
                    raise ValueError(
                        f"{path}:{reader.line_num}: expected department,team,user "
                        "(three non-empty columns and optional subscription key, no header)"
                    )
                if any(not 32 <= ord(char) <= 126 for value in values for char in value):
                    raise ValueError(
                        f"{path}:{reader.line_num}: header values must be printable ASCII"
                    )
                key = values[3] if len(values) == 4 and values[3] else None
                if key is not None and len(key) > 256:
                    raise ValueError(
                        f"{path}:{reader.line_num}: subscription key exceeds 256 chars"
                    )
                identities.append(Identity(*values[:3], subscription_key=key))
        except csv.Error as exc:
            raise ValueError(f"{path}:{reader.line_num}: invalid CSV: {exc}") from exc
    if not identities:
        raise ValueError(f"{path}: identity file must contain at least one row")
    return identities
