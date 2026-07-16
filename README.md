# ClaudePet 🐾 — 데스크탑을 걸어다니는 Claude 사용량 위젯

귀여운 동물 캐릭터가 작업표시줄 위를 걸어다니며 **Claude(Claude Code) 사용량**을 실시간으로 보여주는 Windows 데스크탑 펫입니다.

- 배에 **현재 세션(5시간) 사용량 %** + 리셋까지 남은 시간 표시
- **클릭**하면 주간 한도 패널 (모든 모델/Opus/Sonnet 사용량 %, 초기화까지 남은 일수·시간)
- 창을 만나면 **타고 올라가거나** 방향을 바꾸고, 창 끝에서는 떨어짐
- **더블클릭**으로 캐릭터 렌덤 교체 (고양이/강아지/펭귄/거북이), 드래그로 이동
- 목도리/사용량 색: 파랑(<50%) → 주황(<80%) → 빨강(80%+)

> [!IMPORTANT]
> ### 🚀 Python 설치 없이 바로 사용하기
> **[📥 Releases에서 ClaudePet.exe 다운로드](https://github.com/kst9920/usage_desktop/releases/latest)**
>
> 설치·컴파일 과정 없이 exe 하나만 받아서 실행하면 끝!
> (캐릭터 스킨 4종 내장 / 실행 조건: PC에 Claude Code 로그인)
>
> 처음 실행 시 Windows SmartScreen 경고가 뜨면 **"추가 정보" → "실행"**을 누르세요.
> 서명되지 않은 개인 제작 프로그램이라 뜨는 경고입니다.

## 소스로 실행 (개발자용)

```
pip install pillow
pythonw claude_pet.py
```

요구 사항:
- Windows, Python 3.9+
- **Claude Code 로그인** (사용량 데이터를 로그인 토큰으로 조회)

## 배포용 exe 만들기

`build.bat` 실행 → `dist\ClaudePet.exe` 생성 (skin 포함, Python 설치 불필요).
서명되지 않은 exe라 SmartScreen 경고가 뜰 수 있습니다 — "추가 정보 → 실행".

## 커스텀 캐릭터

`skin/` 아래 폴더를 만들고 6개 PNG를 넣으면 자동 인식됩니다.

```
skin/내캐릭터/
  walk1.png walk2.png   걷기 2프레임
  climb1.png climb2.png 벽타기 2프레임
  fall.png              낙하
  idle.png              쉬기
```

정사각형(권장 512×512), 투명 배경, 오른쪽을 보는 그림. 텍스트가 중앙에 얹히므로
중앙부는 밝고 단순하게. 일부 포즈만 넣어도 됩니다.

## 동작 원리 & 주의

- Claude Code가 저장한 OAuth 토큰(`~/.claude/.credentials.json`)으로
  Anthropic의 usage 엔드포인트를 3분 간격으로 조회합니다 (429 시 자동 백오프).
- 토큰이 만료되면 자동 갱신하며, 실패 시 캐릭터에 빨간 점이 표시됩니다
  (Claude Code를 한 번 실행하면 해결).
- **비공식(내부) API**를 사용하므로 Anthropic 정책·API 변경에 따라
  언제든 동작이 중단될 수 있습니다. 개인 용도로만 사용하세요.
- 토큰은 exe나 외부로 전송되지 않으며, 각자 자기 PC의 로그인으로
  자기 계정 사용량만 표시됩니다.

## 라이선스

MIT
