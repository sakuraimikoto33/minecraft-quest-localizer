# OpenAI・設定・ログ

## 設定タブ

設定ウィンドウは次の3タブで構成します。

- 「翻訳」: 原文・翻訳先locale、既存訳の再利用
- 「固有名詞保護」: resource pack走査、走査上限、続行確認
- 「OpenAI」: APIキー、モデル、Fast Mode、timeout、再試行、バッチ、翻訳プロンプト

OpenAIが未設定でも解析と他の設定保存はできますが、翻訳開始にはAPIキーとモデルが必要です。

locale、resource pack走査、固有名詞走査上限を変更すると解析結果を無効にし、再解析を求めます。既存訳の再利用や固有名詞保護の続行確認だけを変更した場合は、解析結果を維持します。

## モデルの取得と保存

「利用可能なモデルを取得」はOpenAIの `GET /v1/models` を呼び出し、そのAPIアカウントで利用できるテキスト生成モデルを表示します。翻訳にはResponses APIのStructured Outputsを使います。

最後に正常取得したモデル一覧と選択モデルは設定へ保存し、次回起動時は一覧を再取得しなくても翻訳できます。APIキーを変更し、保存済みモデルを利用できない場合はOpenAI APIのエラーを表示するため、必要なときだけ一覧を再取得して選び直します。

## Fast Mode

有効な場合、翻訳用Responses POSTリクエストだけに `service_tier: "priority"` を追加します。無効な場合はこのfieldを送信せず、モデル一覧取得にも追加しません。利用可否と追加料金はモデル、OpenAI Project、契約に依存します。

## timeoutと通信再試行

「再試行回数（初回を除く）」は0〜10回で設定できます。接続エラー、timeout、HTTP 408 / 409 / 429 / 5xxの後、短い指数バックオフを置いて失敗した現在のAPIリクエストだけを再送します。TLS証明書の検証失敗は再試行しません。

完了済みの翻訳バッチはメモリに保持し、後続バッチでtimeoutしても最初から送り直しません。ただし、timeoutした1リクエスト内の応答は完了と判断せず、そのバッチを再送します。再試行を使い切った場合は翻訳ファイルを書きません。アプリ終了後まで引き継ぐcheckpointは作りません。

通信再試行と、翻訳結果の構造・保護検証で行う1件ごとの再試行は別です。

## 翻訳プロンプト

`{source_locale}` と `{target_locale}` は実行時のlocaleへ置換します。編集したプロンプトはResponses APIの `instructions` として送信します。

プレースホルダー、装飾、改行、URL、ID等を保持する変更不可の安全protocolは、カスタムプロンプトの場合も必ず追加します。過去の既定文と完全一致する古い設定だけを現行protocolの既定文へ更新し、ユーザーが編集した文は変更しません。

## APIキー

APIキーはログや翻訳ファイルへ書きません。例外文にAPIキー形式の文字が含まれても表示・保存前に伏せます。

- 「Windows DPAPIで暗号化保存」を有効にした場合だけ、現在のWindowsユーザーで復号できる暗号文を `%APPDATA%\MinecraftQuestLocalizer\settings.json` へ保存します。
- 無効時は現在の起動中だけメモリに保持します。
- 環境変数 `OPENAI_API_KEY` も利用できます。環境変数由来のキーは、保存チェックを明示的に有効にしない限りDPAPIへ保存しません。
- 一般設定だけを保存した場合は、環境変数によって上書きされている既存のDPAPIキーを変更しません。

## 解析表示とセッションログ

画面の各警告欄は応答性のため100件まで表示します。省略分を含む解析結果全文と、解析・翻訳の開始/完了、安全確認の再試行、キャンセル、設定・モデル一覧の失敗、その他のエラーを1回の起動につき1つのUTF-8セッションログへ追記します。

保存先:

- Windows: `%APPDATA%\MinecraftQuestLocalizer\logs`
- その他: 設定ファイルと同じconfigディレクトリの `logs`

起動案内の表示時にログを作成し、GUIの「セッションログ」に絶対パスを表示します。同じ起動中の再解析も同じファイルへ追記し、画面表示を置き換えても過去の記録は消しません。JAR単位の高頻度な進捗はセッションログへ書きません。

セッションログにAPIキー、Authorization Bearer値、翻訳プロンプト、暗号化済みAPIキー設定は記録しません。制御文字や不可視文字は読めるエスケープ表記で記録します。本アプリの正式なファイル名とヘッダーを持つ直近10セッションだけを残し、`logs` 内のその他のファイルは削除しません。

## OpenAI参考資料

- [Models API](https://developers.openai.com/api/reference/resources/models/methods/list)
- [Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
