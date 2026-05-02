# Codex CLI Environment Setup

## 1. 基本方針

- Codex CLI は **AiReq（Django）を起動している同じ実行環境** にインストールする。
- Django を Windows で起動する場合は、Windows 側に Codex CLI を入れる。
- Django を WSL/Linux で起動する場合は、WSL/Linux 側に Codex CLI を入れる。
- OS 自動判定ロジックは実装しない。環境ごとの差は `.env` で明示的に切り替える。

## 2. Windows 用設定例

```env
TARGET_REPO_DIR=C:\work\AiReq
AIREQ_CODEX_CLI_COMMAND=C:\Users\<ユーザー名>\AppData\Roaming\npm\codex.cmd
AIREQ_CODEX_CLI_SANDBOX=workspace-write
AIREQ_CODEX_CLI_TIMEOUT=900
```

補足:

- `codex.cmd` は Windows の npm グローバルインストール先に配置されることが多い。
- パスに空白がある場合は、`.env` 側で引用符を含めるか、空白を含まないパスを指定する。
- 例: AIREQ_CODEX_CLI_COMMAND="C:\Program Files\...\codex.cmd"

## 3. WSL/Linux 用設定例

```env
TARGET_REPO_DIR=/home/<user>/work/AiReq
AIREQ_CODEX_CLI_COMMAND=codex
AIREQ_CODEX_CLI_SANDBOX=workspace-write
AIREQ_CODEX_CLI_TIMEOUT=900
```

補足:

- `AIREQ_CODEX_CLI_COMMAND` は `PATH` が通っていれば `codex` でよい。
- `PATH` が通っていない場合は絶対パス（例: `/home/<user>/.npm-global/bin/codex`）を指定する。

## 4. 運用ルール

- 本番/開発/検証で実行環境が異なる場合も、**その環境ごとの `.env`** を用意して切り替える。
- `TARGET_REPO_DIR` は、Django プロセスから見えるパスを指定する。
- Codex CLI の `cwd` は `TARGET_REPO_DIR` 固定で実行されるため、リポジトリ直下を指定する。

## 5. 動作確認チェック

1. Django を起動する環境で `codex --version` が成功すること。
2. `.env` の `AIREQ_CODEX_CLI_COMMAND` がその環境で実行可能であること。
3. AiReq 画面で実行方式を `Codex CLI` にしてジョブを実行し、`stdout/stderr` が `Job.result` に記録されること。
4. 実行後に AiReq 側テスト結果 (`test_passed`) が更新されること。

### 環境切り替え方法

Windows:

```powershell
copy .env.windows .env
```

WSL:

```bash
cp .env.wsl .env
```

※ 変更後は Django を再起動すること
