"""백업 복구 도구:  python restore_backup.py <백업파일.json.gz> [--tables user_data,stocks] [--yes]

  • 아무 옵션 없이 실행하면 '미리보기'만 합니다. (DB 를 건드리지 않음)
  • 실제로 되돌리려면 --yes 를 붙이세요. 백업에 있는 행을 같은 키(기본키) 기준으로 덮어씁니다(upsert).
  • 백업 이후에 새로 생긴 행은 지우지 않습니다. (덮어쓰기만 함)
  • 일부 표만 되돌리려면 --tables 로 지정하세요. (예: --tables user_data)
  • 환경변수 SUPABASE_URL, SUPABASE_KEY 가 봇과 같은 값으로 설정되어 있어야 합니다.
"""
import argparse, gzip, json, os, sys

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--tables", default="")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    with gzip.open(a.file, "rt", encoding="utf-8") as f:
        dump = json.load(f)
    meta, tables = dump["meta"], dump["tables"]
    want = [t.strip() for t in a.tables.split(",") if t.strip()] or list(tables)
    print(f"백업 시각(UTC): {meta.get('created_at')}")
    for t in want:
        if t not in tables:
            print(f"  ! {t}: 백업에 없음 (건너뜀)")
        else:
            print(f"  - {t}: {len(tables[t]):,}행")
    if meta.get("errors"):
        print("  ※ 백업 당시 읽지 못한 표:", ", ".join(meta["errors"]))
    if not a.yes:
        print("\n미리보기만 했습니다. 실제로 되돌리려면 --yes 를 붙여 다시 실행하세요.")
        return
    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    for t in want:
        rows = tables.get(t)
        if not rows:
            continue
        for i in range(0, len(rows), 500):
            sb.table(t).upsert(rows[i:i + 500]).execute()
        print(f"복구 완료: {t} {len(rows):,}행")

if __name__ == "__main__":
    main()
