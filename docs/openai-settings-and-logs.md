# OpenAI・設定・ログ

## 設定タブ

設定ウィンドウは次の4タブで構成します。

- 「翻訳」: 原文・翻訳先locale、既存訳の再利用
- 「固有名詞保護」: resource pack走査、走査上限、続行確認
- 「OpenAI」: API送信先、APIキー、モデル、Fast Mode、timeout、再試行、バッチ、翻訳プロンプト
- 「ログ」: デバッグログの有効・無効

APIが未設定でも解析と他の設定保存はできます。翻訳開始にはモデルIDが必要です。OpenAI公式APIではAPIキーも必要ですが、互換APIでは接続先が許可する場合に限りキーなしで接続できます。

locale、resource pack走査、固有名詞走査上限を変更すると解析結果を無効にし、再解析を求めます。既存訳の再利用や固有名詞保護の続行確認だけを変更した場合は、解析結果を維持します。

## API送信先

既定の `https://api.openai.com/v1` に加え、OpenAI Responses APIと互換性のあるサービスを指定できます。ベースURLはAPIルートまで（通常は `/v1` まで）とし、末尾に `/responses` や `/models` を付けません。本アプリは翻訳に `POST /responses` とStructured Outputs、モデル取得に `GET /models` を使用します。Chat Completions APIには対応していません。

インターネット上の送信先にはHTTPSが必須です。HTTPはlocalhostまたはloopbackアドレスへの接続だけに使用できます。リダイレクトは同一origin内だけを許可し、認証情報を別の送信先へ転送しません。

APIベースURLを変更すると、別サービスへの資格情報やモデル設定の誤送信を防ぐため、入力中のAPIキー、保存指定、モデル候補、選択モデルをクリアし、Fast ModeをOFFにします。入力したAPIキー、翻訳対象、翻訳プロンプトは指定した送信先へ送信されるため、信頼できるサービスだけを指定してください。

## モデルの取得と保存

「利用可能なモデルを取得」は選択した送信先の `GET /models` を呼び出します。互換APIがモデル一覧を提供しない場合は、モデルIDを欄へ直接入力できます。OpenAI公式APIでは取得済みまたは保存済みの候補から選択します。

最後に正常取得したモデル一覧と選択モデルはAPIベースURL単位で保存します。別の送信先のモデル候補を再利用しません。

## Fast Mode

OpenAI公式APIで有効にした場合、翻訳用Responses POSTリクエストだけに `service_tier: "priority"` を追加します。互換APIでは自動的にOFFとなり、fieldを送信しません。利用可否と追加料金はモデル、OpenAI Project、契約に依存します。

## timeoutと通信再試行

「再試行回数（初回を除く）」は0〜10回で設定できます。接続エラー、timeout、HTTP 408 / 409 / 429 / 5xxの後、短い指数バックオフを置いて失敗した現在のAPIリクエストだけを再送します。TLS証明書の検証失敗は再試行しません。

完了済みの翻訳バッチはメモリに保持し、後続バッチでtimeoutしても最初から送り直しません。ただし、timeoutした1リクエスト内の応答は完了と判断せず、そのバッチを再送します。再試行を使い切った場合は翻訳ファイルを書きません。アプリ終了後まで引き継ぐcheckpointは作りません。

通信再試行と、翻訳結果の構造・保護検証で行う1件ごとの再試行は別です。

## 翻訳プロンプト

`{source_locale}` と `{target_locale}` は実行時のlocaleへ置換します。編集したプロンプトはResponses APIの `instructions` として送信します。

プレースホルダー、装飾、改行、URL、ID等を保持する変更不可の安全protocolは、カスタムプロンプトの場合も必ず追加します。過去の既定文と完全一致する古い設定だけを現行protocolの既定文へ更新し、ユーザーが編集した文は変更しません。

## APIキー

APIキーは翻訳ファイルへ書きません。通常ログ、デバッグログ、API通信JSONでは、現在のAPIキー、APIキー形式の文字列、Authorization Bearer値を保存前にマスクします。通信を記録する際は、既知の資格情報fieldも追加でマスクします。

APIキーを入力した場合は `Authorization: Bearer <APIキー>` として送信します。独自の認証headerやquery parameterを必要とするサービスには対応していません。

- 「Windows DPAPIで暗号化保存」を有効にした場合だけ、現在のWindowsユーザーで復号できる暗号文を `%APPDATA%\MinecraftQuestLocalizer\settings.json` へ保存します。
- 無効時は現在の起動中だけメモリに保持します。
- 暗号化したキーはAPIベースURL単位で関連付け、別の送信先へ自動転用しません。
- 環境変数 `OPENAI_API_KEY` はOpenAI公式APIにだけ利用します。互換APIへ自動送信しません。環境変数由来のキーは、保存チェックを明示的に有効にしない限りDPAPIへ保存しません。
- 一般設定だけを保存した場合は、環境変数によって上書きされている既存のDPAPIキーを変更しません。

## 解析表示とセッションログ

画面の各警告欄は応答性のため100件まで表示します。省略分を含む解析結果全文と、解析・翻訳の開始/完了、安全確認の再試行、キャンセル、設定・モデル一覧の失敗、その他のエラーを1回の起動につき1つのUTF-8セッションログへ追記します。各行は `日時 [レベル] [種別] 内容` の連続した形式で、更新ごとの区切りブロックは作りません。

保存先:

- Windows: `%APPDATA%\MinecraftQuestLocalizer\logs`
- その他: 設定ファイルと同じconfigディレクトリの `logs`

起動案内の表示時にログを作成し、GUIの「セッションログ」に絶対パスを表示します。同じ起動中の再解析も同じファイルへ追記し、画面表示を置き換えても過去の記録は消しません。JAR単位の高頻度な進捗はセッションログへ書きません。解析時に表示済みの確認事項は、翻訳完了後に再度表示しません。

通常ログでは、重複言語keyは資産・ファイルごとの件数だけを記録し、key名は記録しません。翻訳プロンプトやAPIの要求・応答も通常ログには含めません。制御文字や不可視文字は読めるエスケープ表記で記録します。本アプリの正式なファイル名とヘッダーを持つ直近10セッションだけを残し、`logs` 内のその他のファイルは削除しません。

## デバッグログ

「ログ」タブの「デバッグログを出力する」を有効にした間だけ、通常ログとは別の `debug-*.log` と `openai-*.json` を `logs` フォルダーに出力します。それぞれ直近10セッションを独立して管理します。

`debug-*.log` には通常ログの内容に加え、重複した言語keyのファイル名、件数、key名全件を記録します。

Models APIとResponses APIの要求・応答全文、および再試行ごとのエラー応答は、1回の起動につき1つの `openai-*.json` へ時系列の `events` 配列として記録します。API通信が発生しなければJSONファイルは作成しません。

APIキー、Authorization、既知の資格情報fieldはマスクします。一方で、調査に必要なクエスト本文、保護tokenの構造、翻訳結果、カスタムプロンプトは記録されます。共有前に内容を確認してください。

## OpenAI参考資料

- [Models API](https://developers.openai.com/api/reference/cli/resources/models/methods/list)
- [Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
- [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
