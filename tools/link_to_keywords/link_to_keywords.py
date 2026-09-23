#!/usr/bin/env python3
"""
링크(유튜브/인스타) → 영상 대사 + 영상찾기 키워드(중국어 간체/일본어/영어 각 5개)

- 유튜브: 링크를 Gemini에 직접 전달 (다운로드 없음)
- 인스타: yt-dlp로 임시 다운로드 → Gemini 업로드 → 처리 후 로컬/원격 파일 모두 삭제
- 화면 출력: 링크 + 키워드만, 링크(제품)별로 한 묶음씩
- 파일 저장: output/<id>.txt, .json (대사·제품명까지 전부)
- 저장 후 파일을 다시 열어 대사·키워드 15개가 실제로 들어있는지 검증 (검증 실패 시 종료코드 1)

사용법:
  python tools/link_to_keywords/link_to_keywords.py <링크1> [<링크2> ...]

필요 환경변수:
  GEMINI_API_KEY   (필수) aistudio.google.com 무료 키
  GEMINI_MODELS    (선택) 쉼표로 구분한 모델 순서. 한도 초과(429) 시 다음 모델로 넘어감
  IG_COOKIES       (선택) 인스타 다운로드가 막힐 때 쓸 cookies.txt 경로
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

DEFAULT_MODELS = "gemini-3.7-flash,gemini-3.5-flash-lite,gemini-2.5-flash"
ROLES = ["name", "local_search", "scene_action", "scene_effect", "feature"]
ROLE_KO = {
    "name": "제품명",
    "local_search": "현지 검색표현",
    "scene_action": "사용 장면",
    "scene_effect": "효과 장면",
    "feature": "핵심 특징",
}
LANGS = [("zh", "중국어"), ("ja", "일본어"), ("en", "영어")]
OUT_DIR = Path(__file__).resolve().parent / "output"

# 번체에만 쓰이는 자주 나오는 글자 (간체 검증용 휴리스틱)
TRADITIONAL_ONLY = set(
    "們個來時會為與這說對發經過還進國學買賣長開關門間問網紗電體頭點無盡從後見現讓應實嗎麼雙邊邏種書車"
    "馬鳥魚貓東陽陰愛氣風雲紅綠藍廚鍋盤櫃櫥淨潔濕瀝鏽鋼鐵鋁貼換廣場當專業"
)

PROMPT = """You are analyzing a short-form product video for a Korean shopping-shorts creator.

1) transcript: Write the full spoken narration word-for-word, in the original language (usually Korean), in order.
   If a word is unclear, write your best guess; do not summarize. If there is no speech, return the on-screen text.
2) product: One short Korean line naming the product shown.
3) keywords: For finding SIMILAR product footage on Douyin/Xiaohongshu (Chinese), Japanese TikTok/Instagram (Japanese),
   and TikTok/YouTube (English), give exactly 5 search keywords per language, in this exact role order:
   - name: the product's common name/category as locals search it
   - local_search: a phrase locals actually type when looking for this kind of item (platform slang ok)
   - scene_action: the key USE action shown in the video
   - scene_effect: the key RESULT/before-after shown in the video
   - feature: the main differentiating feature
   Chinese MUST be Simplified Chinese. Japanese must be natural Japanese. English lowercase is fine.
   Each keyword: 1-6 words, no hashtags, no emojis.
Return JSON only."""

SCHEMA = {
    "type": "object",
    "properties": {
        "transcript": {"type": "string"},
        "product": {"type": "string"},
        "keywords": {
            "type": "object",
            "properties": {
                lang: {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": ROLES},
                            "kw": {"type": "string"},
                        },
                        "required": ["role", "kw"],
                    },
                }
                for lang, _ in LANGS
            },
            "required": [lang for lang, _ in LANGS],
        },
    },
    "required": ["transcript", "product", "keywords"],
}


class LinkError(Exception):
    """사용자에게 그대로 보여줄 실패 사유"""


# ---------------------------------------------------------------- 링크 판별
def classify(url: str) -> tuple[str, str, str]:
    """(platform, content_id, normalized_url) 반환. 지원 안 하면 LinkError."""
    url = url.strip()
    m = re.search(r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if m:
        vid = m.group(1)
        return "youtube", vid, f"https://www.youtube.com/watch?v={vid}"
    m = re.search(r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:reel|reels|p)/([A-Za-z0-9_-]+)", url)
    if m:
        sc = m.group(1)
        # 사용자명이 들어간 링크는 도구들이 못 읽는 경우가 있어 표준형으로 변환
        return "instagram", sc, f"https://www.instagram.com/reel/{sc}/"
    raise LinkError(f"지원하지 않는 링크입니다 (유튜브/인스타만 가능): {url}")


# ---------------------------------------------------------------- 검증
def validate(data: dict) -> list[str]:
    """문제 목록 반환. 빈 리스트면 통과."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["응답이 JSON 객체가 아님"]
    if not str(data.get("transcript", "")).strip():
        problems.append("대사(transcript)가 비어 있음")
    kws = data.get("keywords") or {}
    for lang, label in LANGS:
        items = kws.get(lang) or []
        if len(items) != 5:
            problems.append(f"{label} 키워드가 5개가 아님 ({len(items)}개)")
            continue
        roles = [i.get("role") for i in items]
        if roles != ROLES:
            problems.append(f"{label} 역할 순서가 다름: {roles}")
        for i in items:
            kw = str(i.get("kw", "")).strip()
            if not kw:
                problems.append(f"{label} 빈 키워드 있음")
            elif lang == "zh":
                bad = [c for c in kw if c in TRADITIONAL_ONLY]
                if bad:
                    problems.append(f"중국어에 번체 글자 포함: {kw} ({''.join(bad)})")
                if not re.search(r"[一-鿿]", kw):
                    problems.append(f"중국어 키워드에 한자가 없음: {kw}")
            elif lang == "ja" and not re.search(r"[぀-ヿ一-鿿]", kw):
                problems.append(f"일본어 키워드에 일본 문자가 없음: {kw}")
            elif lang == "en" and not re.fullmatch(r"[A-Za-z0-9 .,'&+/-]+", kw):
                problems.append(f"영어 키워드에 영문 외 문자: {kw}")
    return problems


# ---------------------------------------------------------------- 출력 형식
def render(url: str, data: dict) -> str:
    lines = [f"[링크] {url}", "", f"[제품] {data.get('product', '').strip()}", "", "[대사]",
             data["transcript"].strip(), ""]
    for lang, label in LANGS:
        kws = " / ".join(i["kw"].strip() for i in data["keywords"][lang])
        lines += [f"[{label}]", kws, ""]
    return "\n".join(lines).rstrip() + "\n"


def render_short(url: str, data: dict) -> str:
    """화면·카톡용: 링크 + 키워드만 (대사는 파일에만 저장)
    링크는 첫 줄에 단독으로 + 다음 줄 비움 → 카톡 등에 붙여넣으면 모바일에서 바로 눌리는 링크가 됨"""
    lines = [url.strip(), ""]
    for lang, label in LANGS:
        lines.append(f"[{label}] " + " / ".join(i["kw"].strip() for i in data["keywords"][lang]))
    return "\n".join(lines) + "\n"


def save_and_verify(content_id: str, url: str, data: dict, meta: dict) -> Path:
    """저장 후 디스크에서 다시 읽어 실제로 들어있는지 확인."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = OUT_DIR / f"{content_id}.txt"
    json_path = OUT_DIR / f"{content_id}.json"
    text = render(url, data)
    txt_path.write_text(text, encoding="utf-8")
    json_path.write_text(json.dumps({"source_url": url, **data, "meta": meta},
                                    ensure_ascii=False, indent=2), encoding="utf-8")

    # --- 재검증: 파일을 다시 열어 확인 ---
    reread = txt_path.read_text(encoding="utf-8")
    missing = []
    if data["transcript"].strip()[:30] not in reread:
        missing.append("대사")
    for lang, _ in LANGS:
        for i in data["keywords"][lang]:
            if i["kw"].strip() not in reread:
                missing.append(i["kw"])
    rejson = json.loads(json_path.read_text(encoding="utf-8"))
    if validate(rejson):
        missing.append("json 재검증 실패")
    if missing:
        raise LinkError(f"저장 파일 재확인 실패 — 빠진 항목: {missing}")
    return txt_path


# ---------------------------------------------------------------- 인스타 다운로드
def download_instagram(url: str, workdir: Path) -> Path:
    if not shutil.which("yt-dlp"):
        raise LinkError("yt-dlp가 설치돼 있지 않음 (pip install yt-dlp)")
    cmd = ["yt-dlp", "-f", "mp4/best", "--no-playlist", "-o", str(workdir / "%(id)s.%(ext)s"), url]
    cookies = os.environ.get("IG_COOKIES")
    if cookies and Path(cookies).exists():
        cmd[1:1] = ["--cookies", cookies]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    files = [p for p in workdir.iterdir() if p.suffix in (".mp4", ".webm", ".mov")]
    if r.returncode != 0 or not files:
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        hint = ""
        if any(k in " ".join(tail).lower() for k in ("login", "rate", "403", "cookies")):
            hint = " → 인스타가 막은 것. IG_COOKIES에 쿠키 파일을 지정하면 해결될 수 있음"
        raise LinkError("인스타 다운로드 실패: " + " | ".join(tail) + hint)
    return files[0]


# ---------------------------------------------------------------- Gemini 호출
def call_gemini(client, models: list[str], parts: list, log: list) -> tuple[dict, str]:
    from google.genai import types
    from google.genai import errors as gerr

    last_err = None
    for model in models:
        feedback = ""
        for attempt in (1, 2):  # 검증 실패 시 1회 재요청
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[*parts, PROMPT + feedback],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=SCHEMA,
                        temperature=0.3,
                    ),
                )
            except gerr.APIError as e:
                code = getattr(e, "code", None)
                log.append(f"{model} 시도{attempt}: API 오류 {code} {str(e)[:120]}")
                last_err = e
                if code in (429, 404, 503):  # 한도초과/모델없음/과부하 → 다음 모델
                    break
                raise LinkError(f"Gemini 오류 ({model}): {e}") from e
            try:
                data = json.loads(resp.text)
            except (json.JSONDecodeError, TypeError):
                log.append(f"{model} 시도{attempt}: JSON 파싱 실패")
                feedback = "\n\nYour previous reply was not valid JSON. Return JSON only."
                continue
            problems = validate(data)
            log.append(f"{model} 시도{attempt}: 검증 문제 {len(problems)}건 {problems}")
            if not problems:
                return data, model
            feedback = "\n\nFix these problems from your previous answer: " + "; ".join(problems)
            last_err = LinkError("; ".join(problems))
    raise LinkError(f"모든 모델 실패. 마지막 오류: {last_err}")


def process(url: str, client, models: list[str]) -> str:
    from google.genai import types

    platform, cid, norm = classify(url)
    log: list[str] = [f"플랫폼={platform} id={cid}"]
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="l2k_"))
    uploaded = None
    try:
        if platform == "youtube":
            parts = [types.Part.from_uri(file_uri=norm, mime_type="video/mp4")]
        else:
            video = download_instagram(norm, tmp)
            log.append(f"다운로드 {video.stat().st_size/1e6:.1f}MB")
            uploaded = client.files.upload(file=str(video))
            for _ in range(60):  # 최대 약 2분 대기
                state = getattr(uploaded.state, "name", str(uploaded.state))
                if state == "ACTIVE":
                    break
                if state == "FAILED":
                    raise LinkError("Gemini가 영상 파일 처리에 실패함")
                time.sleep(2)
                uploaded = client.files.get(name=uploaded.name)
            else:
                raise LinkError("Gemini 파일 처리 대기 시간 초과")
            parts = [types.Part.from_uri(file_uri=uploaded.uri, mime_type=uploaded.mime_type)]

        data, model = call_gemini(client, models, parts, log)
        meta = {"platform": platform, "model": model, "seconds": round(time.time() - t0, 1),
                "created": datetime.now().isoformat(timespec="seconds"), "log": log}
        path = save_and_verify(cid, url, data, meta)
        print(f"# ✅ 저장·재확인 완료: {path}  (모델 {model}, {meta['seconds']}초)", file=sys.stderr)
        return render_short(url, data)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # 로컬 영상 삭제
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)  # Gemini 쪽 사본 삭제
            except Exception:
                pass
        for line in log:
            print(f"#   {line}", file=sys.stderr)


def main(argv: list[str]) -> int:
    urls = [a for a in argv if a.startswith("http")]
    if not urls:
        print(__doc__)
        return 2
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("❌ GEMINI_API_KEY 환경변수가 없습니다.", file=sys.stderr)
        return 2
    from google import genai

    client = genai.Client(api_key=key)
    models = [m.strip() for m in os.environ.get("GEMINI_MODELS", DEFAULT_MODELS).split(",") if m.strip()]
    ok = fail = 0
    for n, u in enumerate(urls, 1):
        print(f"===== {n}/{len(urls)} =====")
        try:
            print(process(u, client, models))
            ok += 1
        except LinkError as e:
            print(f"❌ {u}\n   {e}\n")
            fail += 1
    print(f"# 결과: 성공 {ok} / 실패 {fail}", file=sys.stderr)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
