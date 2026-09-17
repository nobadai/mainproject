"""보고서 종류 → 화면 컴포넌트 → PDF 파일 이름 **배선을 소스로 고정한다.**

★ **왜 백엔드 테스트가 프론트 소스를 보는가.** 이 저장소에는 프론트 테스트 러너가 없다
  (`frontend/package.json` 의 스크립트는 dev · build · start · lint 넷뿐). 러너를 새로
  들이는 것은 이번 PR 범위 밖이라, 화면 소스를 **읽어서** 계약만 잠근다 —
  `tests/api/test_shown_run.py` 가 `frontend/src/lib/demo_as_of.ts` 를 보는 것과 같은 방식이다.

🔴 **여기서 잠그는 것은 «누가 어느 보고서를 그리고 어떤 이름으로 내려받나» 하나다.**
   배선이 빠진 종류는 남의 보고서로 새거나(잘못된 컴포넌트) 남의 이름으로 저장된다
   (잘못된 slug). 눈으로는 바로 안 보이고, PDF 를 열어 봐야 알게 된다.
"""

from __future__ import annotations

import re
from pathlib import Path

_FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
_REPORT_DOWNLOAD = _FRONTEND / "src" / "components" / "ReportDownload.tsx"


def _source() -> str:
    assert _REPORT_DOWNLOAD.exists(), f"경로가 틀렸다: {_REPORT_DOWNLOAD}"
    return _REPORT_DOWNLOAD.read_text(encoding="utf-8")


def test_보고서_종류마다_그리는_화면과_파일_이름이_하나씩_있다():
    """세 종류가 **각자의** 컴포넌트와 slug 를 갖는다.

    🔴 물류가 빠져 있으면 `DOMAIN_REPORTS[kind]` 가 `undefined` 가 되어 화면이 죽거나,
       종전처럼 `kind === "FINANCE" ? ... : ...` 삼항으로 되돌아가 **물류가 판매 보고서로
       그려진다.**
    """
    source = _source()
    mapping = dict(
        re.findall(
            r"(\w+):\s*\{\s*view:\s*(\w+),\s*slug:\s*\"([\w-]+)\"\s*\}",
            source,
        )
        and [(m[0], (m[1], m[2])) for m in re.findall(
            r"(\w+):\s*\{\s*view:\s*(\w+),\s*slug:\s*\"([\w-]+)\"\s*\}",
            source,
        )]
    )

    assert mapping == {
        "FINANCE": ("FinanceReport", "finance"),
        "SALES": ("SalesReport", "sales"),
        "LOGISTICS": ("LogisticsReport", "logistics"),
    }, f"보고서 배선이 계약과 다르다: {mapping}"

    # 세 화면이 실제로 import 되어 있어야 한다 — 표에만 있고 없는 이름이면 빌드가 깨진다.
    for view in ("FinanceReport", "SalesReport", "LogisticsReport"):
        assert re.search(rf"import\s*\{{\s*{view}\s*\}}\s*from", source), f"{view} import 없음"


def test_파일_이름은_표의_slug_로만_정해진다():
    """🔴 **종류 이름을 파일 이름에 다시 적지 않는다.**

    종전에는 `filename(kind === "FINANCE" ? "finance" : "sales", facts)` 였다. 그 자리에
    종류가 늘 때마다 삼항을 고쳐야 했고, 실제로 물류가 빠져 **판매 이름으로 저장**됐다.
    """
    source = _source()
    assert "filename(slug, facts)" in source
    # 삼항으로 되돌아가지 않았는가.
    assert 'kind === "FINANCE"' not in source


def test_PDF_내려받기_경로가_그대로_있다():
    """#807 이 들인 PDF 파이프라인을 물류 배선이 **밀어내지 않았는가.**

    ★ 머지 충돌이 났던 파일이라, 한쪽을 고르면 조용히 사라지는 자리다.
      「파일 저장」은 브라우저가 첫 내려받기를 막았을 때의 재시도 버튼이다.
    """
    source = _source()
    for token in (
        "createReportPdfBlob",
        "triggerBrowserDownload",
        "preparedPdf",
        "data-report-root",
        "PDF 다운로드",
        "파일 저장",
        "인쇄",
    ):
        assert token in source, f"#807 PDF 경로가 사라졌다: {token}"
