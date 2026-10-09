from claude_metrics.project_paths import canonical_project_root


def test_windows_project_paths_are_case_and_separator_insensitive():
    assert canonical_project_root("C:\\Users\\Example\\Projects\\Learn\\") == (
        "c:/users/example/projects/learn"
    )
    assert canonical_project_root("c:/users/example/projects/learn") == (
        "c:/users/example/projects/learn"
    )


def test_posix_project_paths_remain_case_sensitive():
    assert canonical_project_root("/Users/Example/Learn/") == "/Users/Example/Learn"
