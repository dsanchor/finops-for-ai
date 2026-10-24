import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Identity:
    department: str
    team: str
    user: str

    def headers(self) -> dict[str, str]:
        return {
            "X-Department": self.department,
            "X-Team": self.team,
            "X-User": self.user,
        }


def load_identities(path: Path) -> list[Identity]:
    identities = []
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, strict=True)
        try:
            for row in reader:
                values = [value.strip() for value in row]
                if len(values) != 3 or any(not value for value in values):
                    raise ValueError(
                        f"{path}:{reader.line_num}: expected department,team,user "
                        "(three non-empty columns, no header)"
                    )
                if any(not 32 <= ord(char) <= 126 for value in values for char in value):
                    raise ValueError(
                        f"{path}:{reader.line_num}: header values must be printable ASCII"
                    )
                identities.append(Identity(*values))
        except csv.Error as exc:
            raise ValueError(f"{path}:{reader.line_num}: invalid CSV: {exc}") from exc
    if not identities:
        raise ValueError(f"{path}: identity file must contain at least one row")
    return identities
