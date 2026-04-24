# CODEX_TASK_TEMPLATE.md (AiReq / OpenAI API full-source-output template)

このテンプレートは、AiReq で OpenAI API モデルへ修正依頼するための標準フォーマットです。  
このアプリでは diff はアプリ側で生成するため、モデル出力は対象ファイルの修正後コード全文のみとします。

## 0. Global Rules

- `CODEX_RULES.md` を最優先で遵守すること。
- 推測で実装しないこと。
- `TARGET_SOURCE_CODE` を基準に、必要最小限のみ修正すること。

## 1. Target File

- TARGET_FILE_PATH: {{TARGET_FILE_PATH}}

## 2. Editable Scope

### ALLOWED_FILES
{{ALLOWED_FILES}}

### FORBIDDEN_FILES
{{FORBIDDEN_FILES}}

## 3. Current State (AS-IS)

{{AS_IS}}

## 4. Required Changes

{{TO_BE_REQUIRED}}

## 5. Optional Changes

{{TO_BE_OPTIONAL}}

## 6. Purpose

{{WHY}}

## 7. Input Source Code

以下の `TARGET_SOURCE_CODE` が、現在の対象ファイル全文である。  
この全文をベースに修正し、修正後のファイル全文を返すこと。

## 8. Single File Enforcement

- 出力対象は常に 1 ファイルのみとする。
- 複数ファイル修正要求が来た場合でも、出力対象は 1 ファイルのみとする。
- 出力対象は必ず `TARGET_FILE_PATH` に一致させること。
- 他ファイルのコードを出力してはならない。

## 9. Strict Output Sanitization

- 出力に以下を含めてはならない。
- バッククォート3連 (```)
- ` ```python `
- ` ```diff `
- 任意のコードブロック記法
- 出力は純粋なソースコードのみとする。

## 10. File Identity Rule

- 出力内容は必ず `TARGET_FILE_PATH` の修正後内容でなければならない。
- 別ファイルのコードを生成してはならない。
- ファイル名コメント、パスコメント、メタ情報行を出力してはならない。

## 11. Full Output Integrity Rule

- 出力は必ずファイル全体を完全に含むこと。
- 部分的なコード出力は禁止。
- ファイルの先頭行から末尾行まで欠損なく出力すること。
- 出力途中で終了してはならない。

## 12. Structural Completeness Rule

- 出力コードは構文的に完全であること。
- 括弧、クォート、インデントが崩れていないこと。
- 不完全な文、途中で切れた行を出力してはならない。

## 13. Safe Overwrite Rule

- 出力は既存ファイルの完全な置き換えとして使用される前提である。
- 不完全な出力は重大な破壊を引き起こすため、完全でない場合は出力してはならない。

## 14. Hard Constraints

- 対象ファイル以外の内容を出力しないこと。
- 説明文、設計意図、要約、見出し、Markdown、コードブロックを出力しないこと。
- diff 形式を出力しないこと。
- 関数名、クラス名、公開インターフェースは、明示指示がない限り変更しないこと。
- 余計なリファクタリングをしないこと。
- import 整理は必須時のみ行うこと。
- コメント追加・削除は明示指示がある場合のみ行うこと。
- 空白・改行だけの変更をしないこと。

## 15. Output Format (absolute)

- 出力は **修正後コード全文のみ**。
- 先頭文字から末尾文字まで、`TARGET_FILE_PATH` の新しいファイル内容だけを返すこと。
- Python として構文的に有効なコードを返すこと。
- 既存のファイル末尾改行方針を維持すること。

## 16. Safe Failure Mode

- 出力の完全性を保証できない場合は、変更せず `TARGET_SOURCE_CODE` をそのまま返すこと。
- 修正により構造が不安定になる場合は、変更せず `TARGET_SOURCE_CODE` をそのまま返すこと。
- 修正対象が複数ファイルにまたがる場合は、変更せず `TARGET_SOURCE_CODE` をそのまま返すこと。
- 要件が曖昧で安全に変更を確定できない場合は、変更せず `TARGET_SOURCE_CODE` をそのまま返すこと。
- 説明文は返してはならない。
