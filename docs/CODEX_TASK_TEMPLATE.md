# CODEX_TASK_TEMPLATE.md (AiReq / OpenAI API multi-file JSON output template)

このテンプレートは、AiReq で OpenAI API モデルへ修正依頼するための標準フォーマットです。  
このアプリでは、モデル出力は複数ファイル対応の JSON とし、アプリ側が JSON を解析して対象ファイルを上書きします。

## 0. Global Rules

- `CODEX_RULES.md` を最優先で遵守すること。
- 推測で実装しないこと。
- 変更は最小限に限定すること。

## 1. Target App

- TARGET_APP: {{TARGET_APP}}

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

## 7. Target Files Source Code

{{TARGET_FILES_SOURCE_CODE}}

## 8. Output Format (absolute)

- 出力は JSON オブジェクトのみ。
- 形式は次に厳密一致させること。
- `{"files":[{"path":"...","content":"..."}]}`
- `files` は最大 3 要素まで。
- `path` は相対パスのみ。
- `content` は各ファイルの修正後全文。

## 9. JSON Safety Rules

- `content` 内のダブルクォート (`"`) は必ずエスケープすること。
- `content` 内の改行は `\n` としてエスケープすること。
- `content` 内のバックスラッシュは適切にエスケープすること。
- JSON が壊れる可能性がある場合は `{"files": []}` を返すこと。

## 10. File Integrity Rule

- 各 `file` 要素は独立して完全であること。
- `content` が空文字列の場合は禁止。
- `content` が不完全な場合は禁止。
- 1つでも不正な `file` がある場合は `{"files": []}` を返すこと。

## 11. Atomic Safety Rule

- `files` 配列は「全成功 or 全失敗」とする。
- 一部だけ成功は許可しない。
- 1つでも安全に生成できない場合は全体を `{"files": []}` にする。

## 12. Size Safety Rule

- 各 `content` は極端に短い場合は禁止。
- `content` が 10 文字未満の場合は `{"files": []}` を返すこと。

## 13. Output Restrictions

- Markdown 禁止。
- 説明文禁止。
- コードブロック禁止。
- diff 禁止。
- trailing comma 禁止。
- JSON parse 可能であること。

## 14. Safe Failure Mode

- 不明点があり安全に修正できない場合は `{"files": []}` を返すこと。
- 3ファイル以内に収まらない場合は `{"files": []}` を返すこと。
- `ALLOWED_FILES` 外の変更が必要な場合は `{"files": []}` を返すこと。
- 説明文は返してはならない。
