---
name: TOPIX の見直しを指数イベント台帳へ登録する（年次）
about: 毎年 10 月の TOPIX 定期入替・移行措置の再評価を、反映日の前営業日までに指数イベント台帳へ登録する（#1014・ADR-0068）
title: "[ops] TOPIX の見直しを指数イベント台帳へ登録する: <キー（例: topix-2028-10）>"
labels: ["ops", "priority:high"]
---

## 対象

- 見直しのキー: `<topix-YYYY-10 / topix-YYYY-10-review>`（`index_events.EXPECTED_REVIEWS` のキー）
- 種類: 定期入替 / 移行措置の再評価（2027-10 は再評価だけ・2028-10 から毎年の定期入替）
- 公表日: `<YYYY-MM-DD>`（10 月第 5 営業日の見込み）・反映日: `<YYYY-MM-DD>`（10 月最終営業日）
- 公表の URL: <JPX のニュースの URL>

watchdog（`scripts/check_batch_freshness.py`）は、公表見込み日 + 7 日を過ぎても台帳に登録されていなければ
`[ops] TOPIX の見直しが指数イベント台帳に未登録: <キー>` を起票し、登録されれば自動で閉じる。

## 手順

- [ ] 1. 公表の URL と公表日を上に記録する
- [ ] 2. 資料を DL する（**Claude が行い、都度ファイル名・取得元・サイズを示して確認を取る**）。置き場は
      `.index_lists/raw/<公表日 YYYYMMDD>/`、ファイル名は `元名_YYYYMMDD`（DL 日）。一覧は無断転載不可なので
      **コミットしない**（`.gitignore`）。sha256 をこの Issue のコメントに残す
- [ ] 3. `index_events.py` の登録表を更新する: 項目を `EXPECTED_REVIEWS` から `REVIEWS` へ移す（公表日・引け後か・
      反映日・元資料の相対パスと sha256・出典 URL・節の見出し番号と種別・種別ごとの件数・`confirmed`・根拠）。
      係数の道筋（`SCHEDULES`）と、結果を待つ見直し（`PENDING_REVIEWS`）も合わせて直す。次の見直しを
      `EXPECTED_REVIEWS` に足す（月を定数で持たない）
- [ ] 4. `python -I scripts/import_index_list.py <キー>` で取り込み、出た正規化 CSV の sha256 を登録表へ書く。
      件数が公表値と合うこと（合わなければ止まる）
- [ ] 5. 低減・解除の日程を一次資料（算出要領の表）で確かめ、日付の根拠（`primary` / `rule`）と `confirmed` を付ける。
      事前登録文書（`docs/prereg/`）の暦と食い違えば、`tests/test_index_events.py` が落ちる
- [ ] 6. **反映日の前営業日までに**マージする（main への PR → CI → マージ）
- [ ] 7. 反映後、data_j（`jpx_listing_snapshots` の次の版）とウエイト CSV で反映を検算する（#1012 と同じ手順）
- [ ] 8. 非定期追加（2026-11 以降の新規上場・#1023）が期間中にあったかを確かめる
- [ ] 9. 前年のコホート（52 週を経た群）の計測結果を親 #1001 へ積む

## 完了条件

- 台帳の `REVIEWS` に登録され、`python -m scripts.index_exposure` が新しい群で動く
- watchdog の未登録の起票が自動で閉じている
