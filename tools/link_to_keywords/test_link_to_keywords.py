"""오프라인 테스트: 네트워크 없이 가짜 Gemini로 모든 경로 점검.  실행: python -m pytest -q"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import link_to_keywords as L  # noqa: E402

GOOD = {
    "transcript": "와, 이거 보고 진짜 깜짝 놀랐습니다. 동생집 주방이 두 배는 넓어졌더라고요.",
    "product": "싱크대 위 확장형 건조대",
    "keywords": {
        "zh": [{"role": r, "kw": k} for r, k in zip(L.ROLES, ["水槽沥水架", "小厨房收纳神器", "伸缩碗碟架", "砧板刀具收纳", "不锈钢沥水架"])],
        "ja": [{"role": r, "kw": k} for r, k in zip(L.ROLES, ["水切りラック", "キッチン 便利グッズ", "伸縮 ラック", "まな板 収納", "ステンレス ラック"])],
        "en": [{"role": r, "kw": k} for r, k in zip(L.ROLES, ["over the sink dish rack", "kitchen hacks", "expandable rack", "knife holder", "stainless steel rack"])],
    },
}


def clone(d):
    return json.loads(json.dumps(d))


# ---------- 링크 판별
@pytest.mark.parametrize("url,plat,cid", [
    ("https://www.youtube.com/watch?v=FZiOKBQdiaA", "youtube", "FZiOKBQdiaA"),
    ("https://youtube.com/shorts/FZiOKBQdiaA?feature=share", "youtube", "FZiOKBQdiaA"),
    ("https://youtu.be/FZiOKBQdiaA", "youtube", "FZiOKBQdiaA"),
    ("https://www.instagram.com/hello_home___/reel/DX1U1HdT-9q/", "instagram", "DX1U1HdT-9q"),
    ("https://www.instagram.com/reel/DX1U1HdT-9q/?igsh=abc", "instagram", "DX1U1HdT-9q"),
])
def test_classify(url, plat, cid):
    p, c, norm = L.classify(url)
    assert (p, c) == (plat, cid)
    if plat == "instagram":  # 사용자명 링크가 표준형으로 바뀌는지 (vidIQ 실측에서 사용자명 링크 실패했던 건)
        assert norm == f"https://www.instagram.com/reel/{cid}/"


def test_classify_rejects_other():
    with pytest.raises(L.LinkError):
        L.classify("https://www.tiktok.com/@a/video/1")


# ---------- 검증
def test_validate_good():
    assert L.validate(clone(GOOD)) == []


def test_validate_catches_count_traditional_order_empty():
    d = clone(GOOD)
    d["keywords"]["zh"][0]["kw"] = "紗窗防塵網"          # 번체
    d["keywords"]["ja"] = d["keywords"]["ja"][:4]        # 4개
    d["keywords"]["en"][0], d["keywords"]["en"][1] = d["keywords"]["en"][1], d["keywords"]["en"][0]  # 순서
    d["transcript"] = " "
    probs = " ".join(L.validate(d))
    assert "번체" in probs and "5개가 아님" in probs and "역할 순서" in probs and "대사" in probs


def test_validate_simplified_passes_common_shared_chars():
    d = clone(GOOD)
    d["keywords"]["zh"][0]["kw"] = "纱窗防尘网 剪裁 碗"   # 剪·碗은 간체·번체 공통
    assert L.validate(d) == []


# ---------- 저장 후 재확인
def test_save_and_verify(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "OUT_DIR", tmp_path)
    p = L.save_and_verify("abc", "https://youtu.be/x", clone(GOOD), {"m": 1})
    text = p.read_text(encoding="utf-8")
    for lang, _ in L.LANGS:
        for i in GOOD["keywords"][lang]:
            assert i["kw"] in text
    assert json.loads((tmp_path / "abc.json").read_text(encoding="utf-8"))["product"]


# ---------- Gemini 호출: 재요청·모델 전환
class FakeAPIError(Exception):
    def __init__(self, code):
        super().__init__(f"code {code}")
        self.code = code


class FakeModels:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents[-1][-120:]))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return SimpleNamespace(text=step)


@pytest.fixture
def patch_errors(monkeypatch):
    import google.genai.errors as gerr
    monkeypatch.setattr(gerr, "APIError", FakeAPIError)


def test_retry_after_validation_failure(patch_errors):
    bad = clone(GOOD); bad["keywords"]["ja"] = bad["keywords"]["ja"][:3]
    fm = FakeModels([json.dumps(bad), json.dumps(GOOD)])
    log = []
    data, model = L.call_gemini(SimpleNamespace(models=fm), ["m1"], ["part"], log)
    assert model == "m1" and len(fm.calls) == 2
    assert "Fix these problems" in fm.calls[1][1]   # 두 번째 요청에 문제점 피드백이 붙었는지


def test_fallback_to_next_model_on_429(patch_errors):
    fm = FakeModels([FakeAPIError(429), json.dumps(GOOD)])
    data, model = L.call_gemini(SimpleNamespace(models=fm), ["m1", "m2"], ["part"], [])
    assert model == "m2"


def test_all_models_fail(patch_errors):
    fm = FakeModels([FakeAPIError(429), FakeAPIError(429)])
    with pytest.raises(L.LinkError):
        L.call_gemini(SimpleNamespace(models=fm), ["m1", "m2"], ["part"], [])


def test_main_without_key(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert L.main(["https://youtu.be/FZiOKBQdiaA"]) == 2


# ---------- 화면 출력: 링크 + 키워드만
def test_render_short_only_link_and_keywords():
    out = L.render_short("https://youtu.be/x", clone(GOOD))
    first, second = out.splitlines()[:2]
    assert first == "https://youtu.be/x" and second == ""   # 링크 단독 줄 + 빈 줄 (모바일 자동 링크)
    assert "[중국어] 水槽沥水架 / " in out and "[일본어]" in out and "[영어]" in out
    assert GOOD["transcript"][:10] not in out      # 대사는 화면에 안 나옴
    assert len(out.strip().splitlines()) == 5       # 링크 1줄 + 빈 줄 + 언어 3줄


def test_main_prints_one_bundle_per_link(monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(L, "process", lambda u, c, m: L.render_short(u, clone(GOOD)))
    import google.genai as g
    monkeypatch.setattr(g, "Client", lambda api_key: None)
    assert L.main(["https://youtu.be/a", "https://youtu.be/b"]) == 0
    out = capsys.readouterr().out
    assert out.count("=====") == 4 and "1/2" in out and "2/2" in out
    assert out.index("youtu.be/a") < out.index("2/2") < out.index("youtu.be/b")


def test_original_link_kept_as_given():
    # 사용자가 준 링크 그대로 출력 (추적 파라미터 포함해도 그대로) — 받는 사람이 누르면 원래 영상으로
    u = "https://www.instagram.com/hello_home___/reel/DX1U1HdT-9q/"
    assert L.render_short(u, clone(GOOD)).splitlines()[0] == u
