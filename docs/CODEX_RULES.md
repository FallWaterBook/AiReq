# CODEX_RULES.md（YUITO / Codex運用ルール）

この文書は Codex（VSCode拡張）に作業を依頼するための運用ルールです。  
上位規約は `CODING_GUIDELINES.md`（憲法）であり、矛盾する場合は必ず `CODING_GUIDELINES.md` を優先します。  
`.codex-instructions.md` は最小要約、本書は実運用のガードレールです。

---

## 0. 最優先（破ったら差し戻し）
- views.py に業務ロジック禁止（HTTP I/O と service 呼び出しのみ）
- fat model 禁止
- 業務ロジックは services / common_service に集約
- 共通処理を utils に逃がさない（共通化するなら views_helpers / domain_helpers / common_service）
- 論理削除は is_deleted
- 変更は最小（既存 API 仕様・エラーマッピング・テスト期待を壊さない）

## 1. レイヤ責務（迷ったらここに戻る）
- views: HTTP I/O、入力取得、最低限の入力型変換、service呼び出し、レスポンス生成
- views_helpers: 入力パース/軽いバリデーション共通化、例外からHTTPレスポンス変換の集約
- services: CRUD、状態遷移、トランザクション、業務ルール、論理削除、例外送出
- query_service: 参照専用、DTO返却、一覧/詳細の検索条件（論理削除含む）を一元化
- models: フィールド/制約/TextChoices/最低限の補助（業務判断は置かない）

## 2. 例外とHTTP（統一）
- ドメイン例外は `apps/<app>/exceptions.py` に定義し、基底例外（例: `<App>ServiceError`）を継承する
- view は例外を握りつぶさず `map_exception_to_response()` に渡す
- エラーレスポンス形式は常に `{"error": "message"}`

## 3. 日付・時刻・JSONの方針（YUITO標準）
- datetime は ISO8601 文字列で返す（既存実装に合わせる）
- request body は `parse_json_body()` を通す
- 文字列必須は `require_str()`、整数必須は `require_int()`、日付は `require_date()/optional_date()` を使う

## 4. テスト運用ルール
- service 層は必ず unit test を作る（Django TestCaseでDB利用OK）
- 1テスト1検証、テスト名から業務ルールが読めること
- 既存テストが通る前提で、必要なら最小限の修正のみ行う

## 5. 依頼時テンプレ（コピペ用）
以下を依頼文の先頭に必ず付ける：

YUITO の CODING_GUIDELINES.md と CODEX_RULES.md を必ず遵守してください。  
views.py にロジック禁止。fat model 禁止。業務ロジックは services/common_service。  
共通処理を utils に逃がさない。  
既存API仕様・レスポンス形式(`{"error": "..."}`)・例外からHTTPへのマッピング・テスト期待を壊さないのが最優先。  
出力は「設計意図→変更ファイル一覧→修正後コード→テスト要約→テストコマンド」の順で。

## 6. 模範例（recordsを基準にする）
- `apps/records` の責務分離（views / services / views_helpers / query_service / exceptions）を他アプリでも基準にする
- 迷ったら records の実装と同じ方向に寄せる

## 7. 禁止事項（AIがやりがちな地雷）
- views で ORM を直接叩かない（例: `User.objects.get` / `filter`）
- services に寄せられる共通処理を utils に置かない
- 既存テストを削除して通すのは禁止（修正が必要なら理由と影響範囲を書く）
- APIレスポンスのキー名・型・ステータスコードの変更は禁止（変更するなら仕様書とtestsも必ず更新）
- 例外のHTTPマッピング方針（404/409/400）を勝手に変えない

## 8. Done（完了条件 / 完了チェック）
以下をすべて満たしたら完了とする：

- [ ] 変更対象の app で service unit test が追加/更新されている
- [ ] view は HTTP I/O のみで、業務判断・ORM直叩きがない
- [ ] 例外は `apps/<app>/exceptions.py` にあり、views_helpers でHTTPへ変換される
- [ ] `python manage.py test apps -v 2` が通る
- [ ] OpenAPI（`docs/api/*.yaml`）と実装が矛盾していない（必要なら更新）

## 9. 実行コマンド（迷ったらこれだけ）
### 全体テスト（最優先）
- `DJANGO_SETTINGS_MODULE=config.settings.test SECRET_KEY=dummy DEBUG=0 python manage.py test apps -v 2`

### 特定アプリ
- `python manage.py test apps.records -v 2`
- `python manage.py test apps.trainings -v 2`
- `python manage.py test apps.plans -v 2`

### マイグレーション（新規モデル追加時のみ）
- `python manage.py makemigrations`
- `python manage.py migrate`

### OpenAPIの動作確認（任意）
- `python manage.py runserver`

### runserver の設定例
- `DB_NAME_DEV=yuito_dev DB_USER=yuito DB_PASSWORD=CHANGE_ME_STRONG_PASSWORD DB_HOST=127.0.0.1 DB_PORT=5432 DJANGO_SETTINGS_MODULE=config.settings.dev python manage.py runserver`

## 10. READMEへのリンク（任意だが推奨）
README の Documentation に以下を追加：

- Codex運用ルール
