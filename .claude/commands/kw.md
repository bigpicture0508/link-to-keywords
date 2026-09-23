---
description: 유튜브/인스타 링크 → 대사 + 영상찾기 키워드(중·일·영 5개씩)
argument-hint: <링크1> [링크2 ...]
---
아래 명령을 그대로 실행하고, 표준출력(stdout)을 `===== n/N =====` 구분선 기준으로 나눠 **링크(제품) 하나당 코드블록 하나씩** 보여줘(복사 버튼용). 내용은 수정·요약 없이 그대로 (구분선 줄은 빼도 됨). 각 코드블록 바로 위에는 그 링크를 일반 텍스트로 한 줄 적어서 화면에서도 바로 누를 수 있게 해줘.
표준에러(stderr)의 `#` 로그는 끝에 "처리 기록"으로 짧게 붙여줘. 실패한 링크는 실패 이유를 그대로 보여줘.

```
pip install -q -r tools/link_to_keywords/requirements.txt 2>/dev/null; python tools/link_to_keywords/link_to_keywords.py $ARGUMENTS
```

규칙:
- 키워드나 대사를 네가 새로 만들거나 고치지 말 것. 스크립트 출력만 전달.
- GEMINI_API_KEY 없음 / 403 / 네트워크 차단 오류가 나오면, 클라우드 환경 설정(환경변수·네트워크 허용 도메인)을 확인하라고 안내할 것.
- 결과 파일(tools/link_to_keywords/output/)은 커밋하지 말 것.
