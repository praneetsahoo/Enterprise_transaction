"""Phase 12: security regressions."""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _dashboard_helpers():
    """Load only the two pure helpers from the dashboard (importing the page would render it)."""
    src = (ROOT / "app" / "dashboard.py").read_text()
    tree = ast.parse(src)
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in ("spreadsheet_safe", "md_escape"))
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_FORMULA_START" for t in n.targets))]
    ns = {"pd": pd}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "dashboard_helpers", "exec"), ns)
    return ns["spreadsheet_safe"], ns["md_escape"]


def test_downloads_neutralise_spreadsheet_formulas_but_keep_numbers():
    safe, _ = _dashboard_helpers()
    df = pd.DataFrame({"v": ['=HYPERLINK("http://evil","x")', "+1+1", "@SUM(A1)", "-150.00", "-1,250.5", "abc", None],
                       "n": [1, 2, 3, 4, 5, 6, 7]})
    out = safe(df)
    assert list(out["v"])[:3] == ['\'=HYPERLINK("http://evil","x")', "'+1+1", "'@SUM(A1)"]
    assert list(out["v"])[3:6] == ["-150.00", "-1,250.5", "abc"] and pd.isna(out["v"].iloc[6])
    assert list(out["n"]) == [1, 2, 3, 4, 5, 6, 7]


def test_markdown_from_data_cannot_inject_links():
    _, esc = _dashboard_helpers()
    assert esc("U1](http://evil)") == r"U1\]\(http://evil\)"
    assert esc("U9001") == "U9001"


def test_no_assert_in_application_code():
    """`python -O` strips asserts — a business check must never be an assert."""
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text())
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.Assert)], path


def test_sql_values_are_always_bound_parameters():
    """No value is ever formatted into SQL text: f-strings next to SQL may only contain fixed names."""
    allowed = {"app/database/loader.py": {"TXN_FIELDS"}}            # column list from a constant
    for path in (ROOT / "app").rglob("*.py"):
        rel = str(path.relative_to(ROOT))
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in ("text", "execute"):
                for arg in node.args[:1]:
                    if isinstance(arg, ast.JoinedStr):
                        names = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
                        assert names <= allowed.get(rel, set()) | {"f", "join"}, (rel, names)


def test_no_credentials_in_tracked_files():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert not [f for f in files if re.search(r"(^|/)\.env$|\.pem$|\.key$|credentials", f)]
    pattern = re.compile(r"AKIA[0-9A-Z]{16}|aws_secret_access_key\s*=|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY")
    for f in files:
        p = ROOT / f
        if p.suffix in {".py", ".sh", ".toml", ".json", ".md", ".txt", ".ini", ".sql", ".example", ".service"}:
            assert not pattern.search(p.read_text(errors="ignore")), f


@pytest.mark.parametrize("path", [".streamlit/config.toml"])
def test_dashboard_hardening_settings(path):
    cfg = (ROOT / path).read_text()
    assert "enableXsrfProtection = true" in cfg and "maxUploadSize = 50" in cfg and 'toolbarMode = "viewer"' in cfg
