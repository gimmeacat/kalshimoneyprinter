import csv
import json
from datetime import datetime
from pathlib import Path

from kalshi_client import KalshiClient

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ARCHIVE_DIR = DATA_DIR / "archive"

DATA_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)



# ============================================================
# HELPERS
# ============================================================

def save_csv(filename, rows):
    filename = Path(filename)
    filename.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        print(f"{filename}: 저장할 데이터가 없습니다.")
        return

    fieldnames = sorted(
        {
            key
            for row in rows
            for key in row.keys()
        }
    )

    with open(
        filename,
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
        )

        writer.writeheader()

        for row in rows:
            writer.writerow(row)

    print(
        f"✅ 저장 완료: {filename} "
        f"({len(rows)} rows)"
    )


def flatten_row(row):
    """
    dict/list 값이 있으면 CSV에 넣기 쉽게 JSON 문자열로 변환.
    """

    flattened = {}

    for key, value in row.items():

        if isinstance(
            value,
            (dict, list),
        ):
            flattened[key] = json.dumps(
                value,
                ensure_ascii=False,
            )

        else:
            flattened[key] = value

    return flattened


# ============================================================
# PAGINATED DOWNLOAD
# ============================================================

def download_all(
    client,
    endpoint,
    list_key,
    params=None,
):

    if params is None:
        params = {}

    all_rows = []

    cursor = None

    while True:

        request_params = dict(
            params
        )

        request_params[
            "limit"
        ] = 1000

        if cursor:
            request_params[
                "cursor"
            ] = cursor


        response = client.request(
            "GET",
            endpoint,
            params=request_params,
        )


        rows = response.get(
            list_key,
            [],
        )

        for row in rows:
            all_rows.append(
                flatten_row(
                    row
                )
            )


        print(
            f"{endpoint} | "
            f"이번 페이지 {len(rows)}건 | "
            f"누적 {len(all_rows)}건"
        )


        cursor = response.get(
            "cursor"
        )


        if not cursor:
            break


    return all_rows


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "=" * 80
    )

    print(
        "KALSHI TRADE HISTORY DOWNLOAD"
    )

    print(
        "=" * 80
    )


    client = KalshiClient()


    # ========================================================
    # FILLS
    # ========================================================

    print(
        "\n체결 내역 다운로드 중..."
    )

    fills = download_all(
        client=client,
        endpoint="/portfolio/fills",
        list_key="fills",
    )


    save_csv(
        DATA_DIR / "kalshi_fills.csv",
        fills,
    )


    # ========================================================
    # SETTLEMENTS
    # ========================================================

    print(
        "\n정산 내역 다운로드 중..."
    )

    settlements = download_all(
        client=client,
        endpoint="/portfolio/settlements",
        list_key="settlements",
    )


    save_csv(
        ARCHIVE_DIR / "kalshi_settlements.csv",
        settlements,
    )


    print(
        "\n"
        + "=" * 80
    )

    print(
        "✅ 다운로드 완료"
    )

    print(
        "생성 파일:"
    )

    print(
        f"  {DATA_DIR / 'kalshi_fills.csv'}"
    )

    print(
        f"  {ARCHIVE_DIR / 'kalshi_settlements.csv'}"
    )

    print(
        "=" * 80
    )


if __name__ == "__main__":
    main()