---
name: telemetry_playbook
description: 동적 텔레메트리 데이터 조회 도구 사용법
priority: 7
tags:
- telemetry
- logs
- metrics
- troubleshooting
enabled: true
requires_tools:
- telemetry_query
source: https://github.com/monocasual/giada
star: 2000
domain: Design & UX
---

# Telemetry Tool Playbook

로그, 메트릭, 플로우 데이터 조회 방법을 안내합니다.

## 1. 사용 가능한 데이터 유형

| data_type | 설명 | 용도 |
|-----------|-----|-----|
| `logs` | Syslog 이벤트 | 오류 추적, 이벤트 분석 |
| `metrics` | 장비 성능 지표 | 리소스 모니터링 |
| `flows` | 트래픽 플로우 | 트래픽 분석 |

## 2. 로그 조회

### 기본 로그 조회
```python
telemetry_query("logs", {
    "device": "PE1",
    "time_range": "1h"  # 최근 1시간
})
```

### 필터 적용
```python
telemetry_query("logs", {
    "device": "PE1",
    "severity": "error",
    "keyword": "BGP",
    "time_range": "24h"
})
```

### severity 옵션
- `debug`, `info`, `warning`, `error`, `critical`

## 3. 메트릭 조회

### CPU/메모리 조회
```python
telemetry_query("metrics", {
    "device": "PE1",
    "metric_type": "cpu",
    "time_range": "1h"
})
```

### 인터페이스 트래픽
```python
telemetry_query("metrics", {
    "device": "PE1",
    "metric_type": "interface_traffic",
    "interface": "GigabitEthernet0/1",
    "time_range": "6h"
})
```

### metric_type 옵션
- `cpu`, `memory`, `interface_traffic`, `interface_errors`

## 4. 플로우 조회

### 특정 IP 간 트래픽
```python
telemetry_query("flows", {
    "src_ip": "10.1.1.0/24",
    "dst_ip": "10.2.2.0/24",
    "time_range": "1h"
})
```

## 5. 시간 범위 형식

| 형식 | 예시 |
|-----|-----|
| 상대 시간 | `1h`, `6h`, `24h`, `7d` |
| 절대 시간 | `2024-01-15T00:00:00Z/2024-01-15T12:00:00Z` |

## 6. Evidence Pack 제한

> ⚠️ 최대 반환 레코드 수:
> - logs: 30개
> - metrics: 20 윈도우
> - flows: 50개

더 많은 데이터가 필요하면 시간 범위를 좁히거나 필터를 추가하세요.