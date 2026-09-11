# 開発・テスト・参考資料

## 開発用起動

Python 3.11以降でeditable installする場合:

```powershell
py -3.11 -m pip install -e .
py -3.11 -m mq_localizer
```

## アダプター構成

翻訳対象形式は `src/mq_localizer/adapters/base.py` の `QuestAdapter` で分離しています。新しい形式はアダプターを実装し、`AdapterRegistry` へ登録します。

- `probe`: 実ファイル配置を確認し、0なら非対応、大きい数ほど高い確信度を返す
- `load`: 原文を変更せず `TranslationProject` / `TranslationUnit` へ抽出する
- `validate_output`: provider呼び出しや書き込み前に出力pathを検証する
- `write`: 選択済みの翻訳値だけを対象形式と出力先に対して検証し、atomic writeする

Responses APIクライアント、固有名詞辞書、placeholder / 装飾保護、ログ、GUIは共通です。Better QuestingやMod単体の言語JSONは現在の対応形式には含まれませんが、既存カテゴリへ対応付けできる場合はアダプター追加で共通パイプラインを利用できます。新しい翻訳カテゴリが必要な場合はカテゴリ定義とGUIも拡張します。

## テスト

```powershell
py -3.11 -B -m unittest discover -s tests -v
```

テストはSNBT / JSON5構文、世代別アダプター、原文hash不変、選択外項目の除外、装飾・改行・placeholder、Mod / Minecraft / KubeJS / resource pack用語、Responses APIモック、raw JSON、出力path検証、atomic write / rollbackを対象にします。実際の外部APIを呼ぶテストは含みません。

Windows x64の単一EXEとGitHub Releaseの条件は[Windows単一ファイルのビルドとRelease](release.md)を参照してください。

## 参考資料

- [FTB Quests公式リポジトリ / CHANGELOG](https://github.com/FTBTeam/FTB-Quests/blob/main/CHANGELOG.md)
- [FTB Quests 1.21 TranslationManager](https://github.com/FTBTeam/FTB-Quests/blob/v2101.1.0/common/src/main/java/dev/ftb/mods/ftbquests/quest/translation/TranslationManager.java)
- [FTB Quests現行TranslationTable](https://github.com/FTBTeam/FTB-Quests/blob/main/common/src/main/java/dev/ftb/mods/ftbquests/quest/translation/TranslationTable.java)
- [FTB Quests現行対応Minecraft版](https://github.com/FTBTeam/FTB-Quests/blob/main/gradle.properties)
- [Minecraft Java Edition 26.1.2公式リリースノート](https://www.minecraft.net/en-us/article/minecraft-java-edition-26-1-2)
- [Minecraft Forge 1.12.x `mcmod.info` 仕様](https://docs.minecraftforge.net/en/1.12.x/gettingstarted/structuring/)
- [OpenAI Models API](https://developers.openai.com/api/reference/cli/resources/models/methods/list)
- [OpenAI Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
