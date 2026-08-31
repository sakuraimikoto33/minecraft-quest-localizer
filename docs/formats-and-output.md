# 対応形式の検出と出力

## 自動検出

入力はModpackのインスタンスルート1つです。Minecraftバージョン、FTB Quests形式、原文localeの実ファイル、出力先は解析時に自動判定します。バージョン文字列だけではなく、実際のquestとlocaleの配置を優先します。

Minecraftバージョンは従来の `1.x` とカレンダー方式の `26.x` を扱います。PrismLauncher / MultiMC、CurseForge、一般的なinstance profile、FTB Quests JARのMinecraft専用fieldを確認し、packやMod自体の `version` をMinecraft版と誤認しません。安全に世代を判別できない旧版の直書きSNBTは、誤変換を避けるため停止します。

FTB Questsのlocale方式はMinecraft 1.21向け `2100.1.0` で導入され、1.21.1向け `2101.x` に継承されました。`26.1.2.1` 以降は分割JSON5形式も扱います。

## 対応形式

| FTB Quests / Minecraft | 入力 | 出力 |
| --- | --- | --- |
| 1.20.x以前・キー化済み | FTB Quest Localizer / KubeJS等の `en_us.json` | 同じlangフォルダーの `ja_jp.json` |
| 1.20.x以前・直書きSNBT | `config/ftbquests/quests` | `quests.bak` + キー化した `quests` + KubeJS言語資産またはinstance内resource pack |
| 1.21系native locale | `lang/en_us.snbt` | `lang/ja_jp.snbt` |
| 1.21系 + FTB Quests Lang Splitter | `lang/en_us/**/*.snbt` | `lang/ja_jp/**/*.snbt` |
| Minecraft 26.1.2 / FTB Quests 26.1.2.1以降 | `lang/en_us/**/*.json5` | `lang/ja_jp/**/*.json5` |

localeは設定で変更できるため、表の `en_us` / `ja_jp` は代表例です。

## 1.21系native SNBT

`config/ftbquests/quests/lang/<原文locale>.snbt` を検出し、同じ `lang` 内へ `<翻訳先locale>.snbt` を出力します。`data.snbt` に `fallback_locale` がある場合は、利用できる原文localeの判定に使います。

配列型の `quest_desc` / `chapter_subtitle` は要素数、順序、空文字の段落を保持します。

## 分割SNBT / JSON5

`lang/<原文locale>` ディレクトリを再帰走査し、翻訳先localeに同じ相対配置で出力します。questディレクトリ内では `lang/<翻訳先locale>` 以外への書き込みを拒否します。JSON5出力はコメントを追加せず、JSON5として有効なstrict JSONで生成します。

## 1.20.x以前のキー化済みJSON

questがすでに `{translation.key}` 化されている場合は、実際のquest SNBTから参照されるkey、FTB固有の配置名、quest keyの内容を使って原文JSONを検出します。最も確かな候補が1つなら自動採用し、同順位が複数ある場合は誤ったJSONへ書き込まず停止します。

## 1.20.x以前の直書きquest SNBT

初回はMinecraftを完全に終了した状態で実行してください。書き込み前に入替え内容とすべての出力先を表示し、現在の `quests` を `quests.bak` へリネームして原本を保存します。その原本から、直書き文字列を決定的な `{mq_localizer...}` keyへ変換した新しい `quests` を作ります。raw JSON text componentは `text` の文字列だけを `translate` へ変換し、色やclick / hover event、URLは保持します。

```text
config/ftbquests/
├─ quests.bak/  # 初回に保存する直書き原本
└─ quests/      # 原本から作るキー化済みquest
```

安全な `kubejs` フォルダーがある場合、言語資産はKubeJSから自動的に読み込まれる場所へ出力します。

`en_us.json` は常に出力します。翻訳先が `en_us` なら翻訳結果、それ以外なら互換用のfallback原文が入ります。原文localeが `en_us` 以外の場合は、その原文localeの言語資産も追加します。

```text
kubejs/assets/mq_localizer/lang/
├─ en_us.json  # fallback原文、または翻訳先がen_usなら翻訳結果
├─ <原文locale>.json  # 原文がen_us以外の場合
└─ <翻訳先locale>.json  # 翻訳先がen_us以外の場合
```

`kubejs` がない場合はinstanceの `resourcepacks` 内に生成します。翻訳後のポップアップに従い、Minecraftのresource pack画面で `mq_localizer_<翻訳先locale>` を有効にしてください。

```text
resourcepacks/mq_localizer_<翻訳先locale>/
├─ pack.mcmeta
└─ assets/minecraft/lang/
   ├─ en_us.json  # fallback原文、または翻訳先がen_usなら翻訳結果
   ├─ <原文locale>.json  # 原文がen_us以外の場合
   └─ <翻訳先locale>.json  # 翻訳先がen_us以外の場合
```

`quests.bak` は再実行で上書きしません。現在の `quests` が `quests.bak` から本ツールが生成する内容と一致する場合は、quest側を変更せず言語資産だけを更新します。両フォルダーが直書き原本に見える場合や、現在の `quests` が別途編集されている場合は上書きせず停止します。

## 翻訳対象と既存データ

クエストブック、チャプターグループ、チャプター、クエスト、タスク、報酬、報酬テーブル、クエストリンクの各title、チャプター・クエストのsubtitle、クエスト詳細、画像hover文、その他の独自keyを個別に選択できます。選択外の既知keyはOpenAIへ送らず、翻訳先localeにも書きません。

配列型の詳細・subtitleは項目単位で選択し、選択した場合は空行を含む配列全体の要素数と順序を保持します。配列の一部だけを翻訳先localeへ出力しません。

既存訳の再利用、選択外項目の除外、手動keyの保持の詳細は[翻訳結果の保護と安全な書き込み](translation-safety.md)を参照してください。
