from collections import Counter
from pathlib import Path

import pytest

from clients.common import main
from clients.identities import Identity, load_identities


def test_load_identities_trims_values_and_supports_bom(tmp_path):
    path = tmp_path / "identities.csv"
    path.write_text(
        "\ufeff finance , globalrisk , david@microsoft.com \r\nretail,cards,user2\r\n",
        encoding="utf-8",
    )
    assert load_identities(path) == [
        Identity("finance", "globalrisk", "david@microsoft.com"),
        Identity("retail", "cards", "user2"),
    ]
    assert load_identities(path)[0].headers() == {
        "X-Department": "finance",
        "X-Team": "globalrisk",
        "X-User": "david@microsoft.com",
    }


@pytest.mark.parametrize(
    "content, message",
    [
        ("", "at least one row"),
        ("\n", "three non-empty columns"),
        ("finance,team\n", "three non-empty columns"),
        ("finance,team,user,extra\n", "three non-empty columns"),
        ("finance, ,user\n", "three non-empty columns"),
        ('finance,team,"user\ninjected"\n', "printable ASCII"),
        ("finance,team,us\u00e9r\n", "printable ASCII"),
        ('finance,team,"unterminated\n', "invalid CSV"),
    ],
)
def test_invalid_identity_file_is_rejected(tmp_path, content, message):
    path = tmp_path / "invalid.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_identities(path)


def test_missing_identity_file_is_rejected(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_identities(tmp_path / "missing.csv")


def test_synthetic_identity_structure():
    path = Path(__file__).resolve().parents[1] / "clients" / "data" / "identities.csv"
    identities = load_identities(path)
    assert len(identities) == 24
    assert len({identity.user for identity in identities}) == 24
    assert all(
        identity.user.count("@") == 1 and identity.user.endswith("@microsoft.com")
        for identity in identities
    )
    departments = Counter(identity.department for identity in identities)
    assert len(departments) == 4
    assert set(departments.values()) == {6}
    teams = Counter((identity.department, identity.team) for identity in identities)
    assert len(teams) == 8
    assert set(teams.values()) == {3}
    for department in departments:
        department_teams = {
            identity.team for identity in identities if identity.department == department
        }
        assert len(department_teams) == 2


@pytest.mark.parametrize("kind", ["risk", "customer"])
@pytest.mark.parametrize("mode", ["random", "sequence"])
def test_identity_cli_options(kind, mode, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "client",
            "--prompt",
            "Hola",
            "--identities-file",
            "synthetic.csv",
            "--identity-mode",
            mode,
        ],
    )
    captured = []

    async def fake_run(args, agent_kind):
        captured.append((args.identities_file, args.identity_mode, agent_kind))
        return 0

    monkeypatch.setattr("clients.common.run", fake_run)
    with pytest.raises(SystemExit) as exc:
        main(kind)
    assert exc.value.code == 0
    assert captured == [(Path("synthetic.csv"), mode, kind)]


def test_random_identity_mode_requires_file(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["client", "--prompt", "Hola", "--identity-mode", "random"])
    with pytest.raises(SystemExit) as exc:
        main("customer")
    assert exc.value.code == 2
    assert "requires --identities-file" in capsys.readouterr().err
