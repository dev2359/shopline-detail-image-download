# Shopline Detail Image Downloader

Shopline 상품 상세페이지에서 상세 이미지 HTML과 실제 이미지 파일을 내려받는 로컬 웹 도구입니다.

메인 도메인을 입력하면 사이트맵에서 전체 상품 상세 URL을 찾고, 특정 상품 URL을 입력하면 해당 상품만 처리합니다. 필요할 경우 썸네일만 다운로드하거나, 외부 이미지 서버 URL 형식으로 `images_detail.html`의 이미지 주소를 변환할 수 있습니다.

## 주요 기능

- Shopline 사이트맵 기반 전체 상품 URL 수집
- 특정 상품 상세 URL 단건 또는 여러 건 처리
- `.ProductDetail-description` 영역의 상세 이미지 추출
- 상품 썸네일 추출 및 `thumb.jpg` 저장
- 상품별 폴더 생성
- 상세 이미지 파일명 순번 저장: `0.jpg`, `1.gif`, `2.png` 등
- 파일 접두어 적용: 예: `Celladix_0.jpg`
- 외부 서버 Base URL, 폴더명, 접두어 기반 HTML URL 치환
- 썸네일 전용 모드 지원
- 로컬 웹 UI와 CLI 실행 지원

## 요구 사항

- Python 3.12 이상 권장
- Playwright
- Playwright Chromium 브라우저

설치:

```powershell
python -m pip install playwright
python -m playwright install chromium
```

## 웹서비스 실행

```powershell
python shopline_image_downloader.py --web --out web_outputs
```

브라우저에서 접속:

```text
http://127.0.0.1:8000
```

포트를 바꾸고 싶으면:

```powershell
python shopline_image_downloader.py --web --host 127.0.0.1 --port 8001 --out web_outputs
```

## 웹 입력 항목

- `메인 도메인`: 예: `https://www.celladix.hk`
- `특정 상품 상세 URL`: 특정 상품만 받을 때 입력합니다. 여러 개 추가할 수 있습니다.
- `사이트맵 URL`: 기본값은 `{메인 도메인}/sitemap.xml`입니다.
- `최대 상품 수`: 테스트용 제한 값입니다. `0`이면 전체 상품을 처리합니다.
- `요청 간 딜레이`: 상품별 요청 사이 대기 시간입니다.
- `외부 서버 Base URL`: 예: `https://objectstorage.ap-chuncheon-1.oraclecloud.com/n/.../b/.../o`
- `폴더명`: 외부 서버에서 브랜드/사이트 단위 폴더명으로 사용합니다. 예: `Celladix_HK`
- `파일 접두어`: 외부 URL과 로컬 파일명에 붙일 접두어입니다. 예: `Celladix_`
- `썸네일만 다운로드`: 체크하면 각 상품 폴더에 `thumb.jpg`만 저장합니다.

## CLI 사용 예시

전체 상품 처리:

```powershell
python shopline_image_downloader.py --base https://www.celladix.hk --out output
```

특정 상품만 처리:

```powershell
python shopline_image_downloader.py --product-url "https://www.celladix.hk/products/example" --out output
```

테스트용으로 1개 상품만 처리:

```powershell
python shopline_image_downloader.py --base https://www.celladix.hk --out output --max-products 1 --debug
```

썸네일만 다운로드:

```powershell
python shopline_image_downloader.py --base https://www.celladix.hk --out output --thumbs-only
```

## 출력 구조

일반 모드:

```text
output/
  상품ID/
    images_detail.html
    product_url.txt
    0.jpg
    1.gif
    2.png
    thumb.jpg
```

썸네일 전용 모드:

```text
output/
  상품ID/
    thumb.jpg
```

상품 폴더명은 우선 `$('#product_id').attr('rel')` 값을 사용하고, 없으면 Shopline 상품 JSON의 `product.id`를 사용합니다.

## 외부 서버 URL 치환

외부 서버 Base URL, 폴더명, 파일 접두어를 입력하면 `images_detail.html`의 이미지 주소를 외부 서버 형식으로 생성합니다.

예시:

```text
Base URL: https://objectstorage.ap-chuncheon-1.oraclecloud.com/n/axgvkldvr9i4/b/isamogu-media-bucket/o
폴더명: Celladix_HK
파일 접두어: Celladix_
상품ID: 9c019a5baad2
```

생성 URL:

```text
https://objectstorage.ap-chuncheon-1.oraclecloud.com/n/axgvkldvr9i4/b/isamogu-media-bucket/o/Celladix_HK/9c019a5baad2/Celladix_0.jpg
```

원본 이미지가 GIF나 PNG이면 확장자는 원본 확장자를 유지합니다.

## 참고

- 상세 이미지는 Playwright로 페이지를 렌더링한 뒤 `.ProductDetail-description` 내부 이미지만 수집합니다.
- 일부 Shopline 페이지는 네트워크 요청이 계속 유지될 수 있어 `networkidle` 타임아웃 시 `domcontentloaded` 방식으로 재시도합니다.
- 너무 짧은 간격으로 대량 실행하면 사이트에서 차단될 수 있으므로 `delay` 값을 적절히 설정하는 것을 권장합니다.
