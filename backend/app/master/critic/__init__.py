r"""★ **`app/critic/` 에서 옮겼다** (2026-09-07 · Critic 은 마스터의 툴이다).

Critic Agent 패키지.

────────────────────────────────────────────────────────────────────────────
🔴 **왜 안 합쳤나** (2026-09-10 실측). 파일이 여덟이라 *"좀 합치지"* 가 나오는 자리다.

① **여덟 다 산 경로다. 죽은 파일 0.**

```text
main.py:56 → router.py → service.py → critic_v0_4.py → critic.py
                                    → llm/judge.py → llm/runtime.py → llm/schemas.py
           → schemas.py → llm/schemas.py
```

  판단 경로도 같은 자리로 들어온다 —
  `master/service.py:103` `MasterVerifier()` → `verifier.py:280` → `service.run_critic_procurement`.

② **큰 둘은 «감싸는» 구조다.** 합치면 **1,575줄 한 파일**이 된다
  (`critic.py` 400 + `critic_v0_4.py` 1,175). 그 결정은 두 파일 머리말에 적혀 있고
  거기가 주인이다 — 여기서 되풀이하지 않는다.

③ **tests 가 이 패키지를 부른다** — 20파일 47임포트.

```bash
grep -rn "^\s*\(from\|import\) app\.master\.critic" tests/ --include=*.py | wc -l
```

  ⚠️ 숫자는 낡는다. 낡았다고 ①②가 뒤집히지 않으니 위 명령으로 다시 재면 된다.

→ **파일 수를 줄이는 것 자체는 값이 아니다.** 여기서 줄어드는 것은 파일 수 하나이고
  늘어나는 것은 한 파일의 줄 수와 그것을 읽는 다음 사람의 품이다.

⚠️ **`critic_v0_4` 라는 이름도 낡은 표시가 아니다.** 설계서 v0.4 를 가리키는 좌표라
  바꾸면 그 연결이 끊긴다.
"""
