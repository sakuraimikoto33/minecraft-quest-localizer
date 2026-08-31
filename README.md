# Minecraft Quest Localizer

MinecraftのModpackで使われるFTB Questsを、OpenAI APIで翻訳するローカルGUIツールです。ModpackのインスタンスルートからMinecraftバージョン、FTB Quests形式、翻訳元、出力先を自動判定します。

選択した項目の翻訳結果だけを翻訳先localeへ書きます。1.20.x以前の直書きquest SNBTだけは、locale化のため原本を `quests.bak` へ保存し、キー化した `quests` を作ります。

現在の翻訳対象はFTB Questsです。Better QuestingやMod単体の翻訳を将来追加できるアダプター構成になっています。

## 必要なもの

- ソースから起動する場合はPython 3.11以降（外部Pythonパッケージは不要）
- OpenAI APIキー

解析はAPIキーなしでも実行できますが、翻訳開始にはOpenAI APIキーと使用モデルの選択が必要です。API利用料金はOpenAIのアカウントと選択モデルに依存します。

## 対応形式

| FTB Quests / Minecraft | 主な入力 | 出力 |
| --- | --- | --- |
| 1.20.x以前・キー化済み | `en_us.json` | 同じlangフォルダーの翻訳先JSON |
| 1.20.x以前・直書きquest SNBT | `config/ftbquests/quests` | `quests.bak`、キー化済み `quests`、KubeJSまたはresource packのlocale |
| 1.21系 native locale | `lang/en_us.snbt` | `lang/<翻訳先locale>.snbt` |
| 1.21.x + FTB Quests Lang Splitter | `lang/en_us/**/*.snbt` | `lang/<翻訳先locale>/**/*.snbt` |
| Minecraft 26.1.2 / FTB Quests 26.1.2.1以降 | `lang/en_us/**/*.json5` | `lang/<翻訳先locale>/**/*.json5` |

`en_us` は代表例です。原文と翻訳先localeは設定画面で選択できます。バージョン文字列だけではなく、実際のquest・localeの配置を優先して形式を判定します。

## 起動

Windows x64では、Releasesから `MinecraftQuestLocalizer-*-windows-x64.exe` をダウンロードして起動できます。

ソースから起動する場合は、リポジトリ直下の `launch.pyw` をダブルクリックします。コンソールから起動する場合:

```powershell
py -3.11 launch.pyw
```

packageとしての起動方法は[開発ドキュメント](docs/development.md)に記載しています。

## 使い方

1. 「設定…」の「OpenAI」タブでAPIキーを入力します。
2. 「利用可能なモデルを取得」を押し、使用するモデルを選択して保存します。
3. 「インスタンスルート」に `config`、`mods` 等が入っているModpackのフォルダーを1つ選びます。
4. 設定で原文・翻訳先locale、メイン画面で翻訳する項目を選びます。
5. 「解析」を押し、検出形式、翻訳元、出力先、固有名詞保護、確認事項を読みます。
6. 問題がなければ「翻訳を開始」を押し、書き込み確認に従います。

1.20.x以前の直書きquest SNBTを初めて処理する前は、Minecraftを完全に終了してください。原本を `quests.bak` に保存してキー化した `quests` を作ります。KubeJSがない場合はresource packへ出力し、翻訳後に有効化を促すポップアップを表示します。

## 翻訳時の保護

Minecraft本体、Mod、KubeJS、および任意でresource packの言語資産を確認し、公式訳がある固有名詞はその訳を使います。公式訳がない、原文と同じ、競合する、または安全に対応付けできない場合は原語を保持します。

次の内容も翻訳前後に検証し、位置と内容を保持します。

- Minecraftの装飾コード、改行、空行
- `%s`、`%1$s`、`{0}`、`${name}` 等のプレースホルダー
- FTB Questsのtemplate、既存のtranslation key
- URL、command、resource ID、quest ID
- raw JSON text componentの構造、style、click / hover event

OpenAIの応答を安全に復元できない場合は対象項目を再試行し、最終的に検証できなければ翻訳ファイルへ書きません。

## 詳細ドキュメント

形式ごとの出力、OpenAI設定とログ、固有名詞、安全検証、拡張方法は[詳細ドキュメント一覧](docs/README.md)を参照してください。
