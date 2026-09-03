import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import list_assigned_issues as lai


def test_filter_assigned_to_keeps_matching_issues():
    issues = [
        {"iid": 1, "title": "A", "assignees": [{"username": "encore"}]},
        {"iid": 2, "title": "B", "assignees": [{"username": "someone_else"}]},
        {"iid": 3, "title": "C", "assignees": []},
    ]

    result = lai.filter_assigned_to(issues, "encore")

    assert [i["iid"] for i in result] == [1]


def test_list_assigned_issues_queries_each_project(monkeypatch):
    calls = []

    def fake_fetch(project_alias, gitlab_api_path=lai.GITLAB_API):
        calls.append(project_alias)
        if project_alias == "harbor":
            return [{"iid": 5, "assignees": [{"username": "encore"}]}]
        return [{"iid": 9, "assignees": [{"username": "someone_else"}]}]

    monkeypatch.setattr(lai, "fetch_open_issues", fake_fetch)

    result = lai.list_assigned_issues(["harbor", "orchard"], "encore")

    assert calls == ["harbor", "orchard"]
    assert result == {
        "harbor": [{"iid": 5, "assignees": [{"username": "encore"}]}],
        "orchard": [],
    }
