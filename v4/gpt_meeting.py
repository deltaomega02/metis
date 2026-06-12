#!/usr/bin/env python3
"""Claude ↔ GPT 멀티턴 전략 회의 도구.

사용: python gpt_meeting.py claude_turn.txt
  - meeting_state.json 에 대화 누적 저장(없으면 system 메시지부터 시작)
  - claude_turn.txt 내용을 사용자(=Claude) 발언으로 추가
  - GPT 호출 → 응답을 state에 저장 + 표준출력으로 출력
파일로 발언을 전달하는 이유: 셸 escaping 회피.
"""
import sys, json, os
from openai import OpenAI

MODEL = "gpt-5.5"
STATE = "meeting_state.json"

SYSTEM = """당신은 GPT다. Claude와 동료로서 자산증식 전략을 *완전 중립적으로* 평가한다.
⚠️ 가장 중요: 적대적으로 반박만 하지도, 칭찬만 하지도 마라. 둘 다(억까·억빠) 명시적으로 경계한다.
장단점을 공정히 저울질 — 근거 있으면 인정, 없으면 기각. 결론을 미리 정해두지 마라.
운영 컨텍스트:
- 매일 회의하며 관여할 의향 있음(완전 방치 아님). 따라서 "운영 복잡/감시 필요"가 무조건 탈락사유는 아님.
- 자본 ~$1,000(곧 키울 수도). 트라우마는 *전략 누적손실*에 한정 — 선물/레버/거래소 자체엔 트라우마 없음. 따라서 "선물이라 위험"식 회피는 금물, 오직 엣지/리스크 수치로만 판단.
- "엣지 없으면 폐기" + 솔직 보고. BH/은행 벤치마크.
임무 2가지:
1. Claude가 가져온 데이터(추세롱 + funding carry 결합 포함)를 *중립적으로* 판정.
2. 우리가 *아직 안 해본* 전략·구조·리스크관리·사이징·결합 아이디어를 **많이**(최소 8개+) 제안. 각각 메커니즘+검증법+예상리스크 1~2줄.
한국어, 핵심 수치 정확히. 마지막에 균형잡힌 권고."""


def main():
    turn_file = sys.argv[1]
    with open(turn_file, encoding="utf-8") as f:
        claude_turn = f.read().strip()

    # 누적 대화 로드 (첫 호출이면 system 메시지로 시작)
    if os.path.exists(STATE):
        msgs = json.load(open(STATE, encoding="utf-8"))
    else:
        msgs = [{"role": "system", "content": SYSTEM}]

    msgs.append({"role": "user", "content": "[Claude]\n" + claude_turn})

    client = OpenAI()
    resp = client.chat.completions.create(
        model=MODEL,
        messages=msgs,
        reasoning_effort="high",
    )
    reply = resp.choices[0].message.content
    msgs.append({"role": "assistant", "content": reply})
    json.dump(msgs, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    u = resp.usage
    print("=" * 90)
    print(f"[{MODEL} 응답]  (tok in {u.prompt_tokens} / out {u.completion_tokens})")
    print("=" * 90)
    print(reply)


if __name__ == "__main__":
    main()
