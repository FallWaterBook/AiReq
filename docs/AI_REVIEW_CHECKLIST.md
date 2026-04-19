cd /home/centos/work/yuito

cat > docs/ai/AI_REVIEW_CHECKLIST.md <<'EOF'
# AI_REVIEW_CHECKLIST.md（YUITO / AIレビュー観点）

この文書は、Codex/ChatGPT に実装・修正を依頼した成果物をレビューするためのチェックリスト。
上位規約は `CODING_GUIDELINES.md` と `docs/ai/CODEX_RULES.md`。

---

## 0. 最重要（ここが崩れたら差し戻し）
- [ ] **views.py に業務ロジックが入っていない**（HTTP I/O + service 呼び出しのみ）
- [ ] **fat model になっていない**（models.py はフィールド/制約中心）
- [ ] **業務ルールは services（write）に集約**されている
- [ ] **参照は query_service（read） + DTO** に分離されている
- [ ] **論理削除は is_deleted** で統一されている
- [ ] **エラーレスポンス形式は {"error": "..."}** に統一されている

---

## 1. レイヤ責務チェック
### views
- [ ] ORM直呼び（Model.objects...）がない
- [ ] パース/必須チェックは views_helpers 経由
- [ ] 例外は握りつぶさず map_exception_to_response に集約

### views_helpers
- [ ] parse_json_body / require_* / optional_* を利用して view の重複が減っている
- [ ] ドメイン例外→HTTP のマッピングが既存ルールと一致（404/409/400/500）

### services（write）
- [ ] @transaction.atomic が適切（作成/更新/削除など）
- [ ] 許可フィールドのみ更新（それ以外は無視＝防御）
- [ ] full_clean() を通して DB制約/バリデーションと整合
- [ ] 存在チェック・削除チェックが service / domain_helpers に寄っている

### query_service（read）
- [ ] DTO を返す（Modelを直接返さない）
- [ ] list/detail の条件が一貫（削除除外、並び順）
- [ ] created_at/updated_at のフォーマットが ISO8601

### domain_helpers
- [ ] get_active_xxx_or_raise / get_user_or_raise が重複排除できている
- [ ] Django例外をそのまま漏らさず、ドメイン例外に変換している

---

## 2. API仕様チェック（OpenAPI & Apidog）
- [ ] OpenAPI（docs/openapi/*.yaml）が実装と一致
- [ ] ステータスコードが一致（特に 404/409/400）
- [ ] 204 No Content のレスポンスに **body を含めていない**
- [ ] Apidog シナリオが仕様通りに通る（作成→取得→更新→削除→削除済みアクセス）

---

## 3. テストチェック（必須）
- [ ] service unit test がある（正常系 + 代表的な異常系）
- [ ] API test がある（疎通 + エラー仕様）
- [ ] 全体テストが通る：`python manage.py test apps -v 2`

---

## 4. 既存互換性チェック（破壊禁止）
- [ ] 既存エンドポイントのパス/メソッドを壊していない
- [ ] レスポンスJSONのキー名を勝手に変えていない
- [ ] 例外→HTTPマッピングを勝手に変えていない
- [ ] 既存テストの期待を壊していない

---

## 5. 変更の最小性（Diffの健康診断）
- [ ] 変更ファイルが必要最小限
- [ ] 目的に対して過剰な抽象化・共通化をしていない
- [ ] utils に逃がしていない（共通化するなら views_helpers/domain_helpers/common_service）

EOF
