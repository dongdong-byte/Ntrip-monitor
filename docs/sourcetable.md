# Sourcetable 조사 결과: mounts.txt 수동 관리를 그만해야 하는 이유

> 상태: **B단계 반영 (Sourcetable 우선, 완전 수신 확인, 보조 목록 자동 갱신)**
> 조사일: 2026-11-25
> 관련 문서: [ini.md](ini.md)

---

## 1. 결론 요약

- gnssdata 캐스터는 **Sourcetable을 정상적으로 준다.** 계정 없이 요청해도 STR 562줄이 온다.
- 예전 주석 "GET / 가 404라서 file이 기본값"은 틀렸다. B단계에서 지웠고, 기본 출처는 `auto`다. 404의 원인은 인증 헤더였다 (2절).
- 예전 감시 목록 `mounts.txt`는 **"2026-07 접속로그 엑셀"에서 뽑은 고정 목록**이었다. 그 이후에 추가한 관측소가 빠져 있었다. 지금은 `mounts/gnssdata.txt`(562개)가 보조 목록이다.
- 관측소를 추가할 때마다 사람이 목록을 손으로 고치는 방식은 누락이 생긴다. **Sourcetable을 주 출처로 쓰고, `mounts/<캐스터명>.txt`는 조회가 실패할 때만 쓴다.**

---

## 2. Sourcetable 요청 테스트

대상: `www.gnssdata.or.kr:2101`, `GET /`, **Authorization 헤더 없음**

| 요청 방식 | 응답 상태 줄 | 크기 | STR 줄 수 |
|---|---|---|---|
| v2: HTTP/1.1 + `Ntrip-Version: Ntrip/2.0` (코드와 같은 헤더) | `HTTP/1.1 200 OK` | 65,089B | 562 |
| v1: HTTP/1.0 (코드와 같은 헤더) | `SOURCETABLE 200 OK` | 65,090B | 562 |
| v1: 최소 요청 (`User-Agent: NTRIP Client 1.0`만) | `SOURCETABLE 200 OK` | 65,090B | 562 |

- 응답 헤더: `Server: NTRIP Caster 1.1`, `Content-Type: text/plain`, `Content-Length: 64937`
- chunked가 아니라 Content-Length 방식이라 현재 `sourcetable.py`의 파싱 방식으로 문제없이 읽을 수 있다.

### 인증 헤더를 붙이면 404 (B단계에서 확인, 2026-11-25)

- 예전 코드는 계정이 설정돼 있으면 `Authorization: Basic ...` 헤더를 붙여 Sourcetable을 요청했다.
- gnssdata는 **Authorization 헤더가 붙으면 계정이 맞든 틀리든 `GET /`에 `HTTP/1.1 404 Not Found`를 돌려준다.** v1, v2, 가짜 마운트 요청 모두 같았다.

  | 요청 (`GET /`, v2) | 응답 |
  |---|---|
  | 인증 헤더 없음 | `HTTP/1.1 200 OK`, STR 562줄 |
  | 실제 계정 | `HTTP/1.1 404 Not Found` |
  | 가짜 계정 `a:b` | `HTTP/1.1 404 Not Found` |
  | 빈 계정 `:` | `HTTP/1.1 404 Not Found` |

- **예전 코드 주석의 "GET /에 404"는 이 동작 때문이었다.** 캐스터가 Sourcetable을 안 주는 게 아니라, 요청 방식의 문제였다.
- 조치: `ntrip/sourcetable.py`가 **인증 없이 먼저 요청**하고, 모두 실패할 때만 인증을 붙여 다시 시도한다. 인증이 있어야 목록을 주는 캐스터에도 대응하기 위해서다.
- 마운트 접속 검사(`GET /마운트명`)에는 지금처럼 인증을 붙인다.
- 완전 수신 판단 근거도 확인했다: 응답 끝이 `ENDSOURCETABLE` 줄로 끝나고, `Content-Length` 헤더가 있다. 캐시 저장 시 이 두 가지로 완전 수신을 판단한다 ([ini.md](ini.md) 7절 8번).

---

## 3. mounts.txt와 Sourcetable 비교

| 구분 | 개수 |
|---|---|
| Sourcetable 마운트 | 562 |
| mounts.txt 마운트 | 508 |
| 양쪽 모두 있음 | 503 |
| **Sourcetable에만 있음 (= 감시 누락)** | **59** |
| **mounts.txt에만 있음 (= 캐스터에 없음)** | **5** |

### 3-1. 감시에서 빠진 59개

```
ANSG-RTCM23   ANSG-RTCM31   BHAO-RTCM30   CHNY-RTCM30   DOND-RTCM23
GANR-RTCM30   GEJE-RTCM30   GGEO-BINEX    GGEO-RTCM23   GGEO-RTCM31
GGEO-RTCM32   GOES-RTCM23   GOJE-BINEX    GOJE-RTCM31   GOJE-RTCM32
GUNW-RTCM30   HPCN-RTCM30   HSDO-BINEX    HSDO-RTCM23   HSDO-RTCM31
HSDO-RTCM32   HWCH-RTCM30   IMSL-RTCM30   JEJU-RTCM32   JINJ-BINEX
JINJ-RTCM23   JINJ-RTCM31   JINJ-RTCM32   JJHG-BINEX    JJHG-RTCM23
JJHG-RTCM31   JJHG-RTCM32   JNDO-RTCM30   MJOO-RTCM30   MKPO-RTCM32
NAJU-RTCM30   NSAN-RTCM23   POCH-RTCM30   SBAO-RTCM23   SBAO-RTCM30
SCHN-RTCM30   SGUI-RTCM30   SGWI-BINEX    SGWI-RTCM23   SGWI-RTCM31
SGWI-RTCM32   SKCH-RTCM30   SWGS-RTCM32   TEAN-RTCM30   ULSN-RTCM30
WLJN-RTCM30   YECH-BINEX    YECH-RTCM23   YECH-RTCM31   YECH-RTCM32
YEJU-RTCM30   YNDK-RTCM30   YNIN-RTCM30   YNJU_RTCM30
```

- `GGEO`, `HSDO`, `JINJ`, `YECH`, `SGWI`는 **몇 개월 전에 추가한 관측소**다. 형식 4종(BINEX, RTCM23, RTCM31, RTCM32)이 통째로 빠져 있다. `GOJE`와 `JJHG`도 같은 패턴이다.
- mounts.txt를 7월 접속로그에서 뽑았기 때문에, 그 시점에 없었거나 접속자가 없었던 마운트는 처음부터 감시 대상에 들어가지 않은 것으로 보인다.

### 3-2. 캐스터에 없는 5개

| mounts.txt | 추정 |
|---|---|
| `GOES-RTCM30` | 캐스터에는 `GOES-RTCM23`만 있음. 형식이 바뀌었거나 폐지된 것으로 보임 |
| `JEJU-RTCM30` | 캐스터에는 `JEJU-RTCM32`가 있음. 형식 변경으로 보임 |
| `MKPO-RTCM30` | 캐스터에는 `MKPO-RTCM32`가 있음. 형식 변경으로 보임 |
| `MLYN-RTCM32` | 캐스터에 없음 |
| `YNJU-RTCM30` | 캐스터에는 **`YNJU_RTCM30`**(언더바)으로 등록됨. 캐스터 쪽 오타로 보임 |

- 매시간 로그에 나오는 `[미등록] HTTP 404 × 3개`가 이 중 일부일 가능성이 높다.
- 매시간 점검 결과는 5개가 아니라 3개다. 나머지 2개는 다른 응답을 받고 있을 수 있으니, 실행 결과에서 정확히 어느 마운트인지 확인한다.
- `YNJU_RTCM30`처럼 **캐스터에 잘못 등록된 이름도 Sourcetable을 쓰면 그대로 따라간다.** 명명 규칙 오류를 별도로 알려주는 기능을 둘지 검토한다.

### 3-3. Sourcetable 내용 분포

**형식 (STR 4번째 필드)**

| 형식 | 개수 |
|---|---|
| RTCM 2.3 | 144 |
| RTCM 3.2 | 137 |
| RTCM 3.0 | 111 |
| BINEX | 103 |
| RTCM 3.1 | 30 |
| RTCM 3.x | 22 |
| RTK | 10 |
| RTCM 3 | 5 |

**인증 (STR 16번째 필드)**

| 값 | 의미 | 개수 |
|---|---|---|
| `B` | Basic 인증 필요 | 515 |
| `N` | 인증 불필요 | 32 |
| (빈 값) | 미기재 | 15 |

- 형식이 `RTK`, `RTCM 3.x`, `RTCM 3`인 항목은 마운트명 규칙(`관측소-형식`)과 맞지 않을 수 있다. 현재 엑셀 writer의 형식별 시트(BINEX, RTCM23, RTCM30, RTCM31, RTCM32)에 분류되지 않을 수 있으니 확인이 필요하다.

---

## 4. 관측소 메타데이터도 빠져 있음

루트의 `station_metadata.json`(관측소 147개, 구현 시 `metadata/gnssdata.json`으로 이동)을 확인한 결과, 새로 추가한 관측소의 한글명과 소속기관이 **등록되어 있지 않다.**

| 관측소 | metadata |
|---|---|
| GGEO | 없음 |
| HSDO | 없음 |
| JINJ | 없음 |
| YECH | 없음 |
| SGWI | 없음 |
| GOJE | 없음 |
| JJHG | 없음 |

- 이 상태로 Sourcetable 감시로 바꾸면 해당 마운트는 그 캐스터 엑셀에서 **`소속미확인` 시트**로 분류된다. 마운트명(영문 코드)은 그대로 남는다.
- Sourcetable에는 위경도와 네트워크명(`KORREF`)은 있지만 **한글 관측소명과 소속기관은 없다.** 한글은 부가 정보라서 사람이 나중에 채워도 된다. 비어 있어도 검사는 진행하고, 미매칭을 메일로 알리지는 않는다.
- 메타데이터는 캐스터별 파일이다. gnssdata에 없는 코드가 서울·해양 캐스터에 있어도 gnssdata 파일에는 넣지 않는다. 자세한 내용은 [ini.md](ini.md) 5절.

---

## 5. 개선 방향

### 5-1. 마운트 목록 출처

- 마운트 목록 출처의 **코드 기본값을 `auto`로 되돌린다.** ini에는 넣지 않는다([ini.md](ini.md) 3절).
  - Sourcetable 조회에 성공하면 그 목록을 사용한다.
  - 조회에 실패하면 보조 파일 `mounts/<캐스터명>.txt`로 대체한다.
  - 진단이 필요하면 CLI `--source file/caster`로 바꿔 실행한다.

### 5-2. 보조 파일을 자동으로 최신화

- 보조 파일과 캐시는 **같은 파일**(`mounts/<캐스터명>.txt`)이다. 별도의 `.cache.txt`는 만들지 않는다.
- Sourcetable을 완전히 수신하고 파싱에 성공할 때마다 이 파일을 자동 갱신한다(임시 파일에 쓴 뒤 교체).
- 조회에 실패하면 **마지막으로 성공했던 목록**을 사용한다.
- 지금 루트의 `mounts.txt`(7월 접속로그 기반)는 구현 시 `mounts/gnssdata.txt`로 옮긴다. 첫 조회에 성공하면 Sourcetable 목록으로 덮어써진다.
- 이렇게 하면 보조 파일도 사람이 관리할 필요가 없다.

### 5-3. 목록 변화 감지

- 직전 실행과 비교해 **새로 생긴 마운트와 사라진 마운트**를 메일에 표시한다.
- 관측소 추가나 폐지, 이름 변경(예: `YNJU_RTCM30`)을 바로 알 수 있다.

### 5-4. 제외 목록 (선택)

- 562개를 전부 감시하면 접속 수가 늘어 503(동시접속 제한)이 더 늘어날 수 있다. 현재 508개 기준으로도 매번 12건이 발생한다.
- 감시하지 않을 마운트가 있다면 캐스터별 제외 패턴을 둔다. 예: `*-RTK`, `TEST*`
- **도입 여부는 미정이다.** 도입할 경우 패턴을 ini에 둘지 코드에 둘지도 그때 결정한다. ini는 단순하게 유지한다는 원칙([ini.md](ini.md) 1절)을 고려한다.

---

## 6. 결정 필요 사항

- [ ] Sourcetable의 562개를 전부 감시할지, 제외할 항목이 있는지 (`RTK` 형식 10개, 인증 `N` 32개 등)
- [ ] 캐스터에 없는 5개(`GOES-RTCM30` 등)를 mounts.txt에서 정리할지. Sourcetable로 전환하면 자동으로 빠진다.
- [ ] `YNJU_RTCM30` 명명 오류를 캐스터 관리 쪽에 알릴지
- [x] 메타데이터는 캐스터별 JSON. 한글명 등록은 서두르지 않음 (GGEO 등 7개 포함)
- [x] 계정을 넣은 상태에서 Sourcetable 요청 재확인: 인증 헤더가 있으면 404. 인증 없이 먼저 요청하도록 수정 (2절)
