# 固有名詞の検出と保護

## 走査する資産

固有名詞保護は、解析時に次のローカル資産を読み取り専用で走査します。ネットワークからMinecraftやModの資産を取得しません。

- Minecraft本体: 検出したMinecraftバージョンに対応するLauncherのversion metadata、client JAR、asset index、content-addressed object
- Mod: `mods/*.jar` のForge / NeoForge `mods.toml`、Fabric、Quilt、legacy `mcmod.info`、JAR Manifest、`assets/<namespace>/lang`
- KubeJS: `kubejs/assets/<namespace>/lang`（常に対象）
- resource pack: `resourcepacks` 直下のフォルダー形式とZIP形式（設定で有効にした場合だけ）

Minecraft資産のmetadataにhashとsizeがある場合は実ファイルを検証します。Minecraft資産がローカルにない場合も、Mod、KubeJS、有効時のresource packから取得できる用語で解析を続けます。

## Mod表示名

Mod metadataから取得した表示名がquest原文に現れた場合、その綴りを原文のまま保持します。大小文字違い、装飾コード、連続するraw JSON text componentをまたぐ表記は追跡しますが、略称や別名は推測しません。

1語のMod名が一般語としても使われる場合は文脈を確認します。例えば `create lava` や `Create a Forgotten Minion` の `create` は翻訳可能な動詞とし、`Build a Create machine` や `Use Create a lot` の `Create` はMod名として保護します。

## 言語資産の優先順位

同じnamespace・同じlanguage keyは次の順で優先し、下位資産で上位の値を上書きしません。

```text
Minecraft本体 > Mod > KubeJS > resource pack
```

- 上位に翻訳先localeの同じkeyがある場合はその値を判定に使い、下位の翻訳先値を参照しません。上位値が原語明記または安全検証で不採用になった場合も、下位の訳で代替せず原語を保持します。
- 上位に翻訳先key自体がない場合だけ、同じ安全な原文表記に対応する次順位の値をfallbackにできます。翻訳先localeだけの値は、上位または同順位に同じ `(namespace, key)` の安全な原文がある場合に限ります。
- 同じ原文表記が別namespace・keyにもある場合、resource IDを特定できない自由文ではその原文を持つ最上位層だけで訳を決めます。
- 同順位の複数資産で安全な訳がすべて一致する場合は統合します。異なる訳、または原語明記と翻訳が競合する場合は走査順を優先せず原語を保持します。
- 1つのJSONまたは`.lang`内でkeyが重複した場合は、記載順を優先せずそのkeyの全出現だけを除外します。通常ログには資産・言語ファイルと重複件数だけを記録し、設定でデバッグログを有効にした場合だけ全keyをデバッグログへ記録します。
- 上位に同じ `(namespace, key)` の原文がある場合、下位の異なる原文表記を独立候補として追加しません。

## 保護状態と続行確認

Mod JARの走査に成功した場合は取得したMod表示名・公式用語の件数を「有効」と表示します。一部資産だけ読めない場合や、Mod以外からだけ用語を得た場合は「一部有効」とし、取得済み用語と未確認範囲を分けて表示します。すべての走査元から保護候補を取得できない場合だけ「利用不可」とします。

JARごとにMod表示名を確認し、1件でも取得できないJARがある場合は既定で続行確認を表示します。「固有名詞保護の確認をスキップする」はダイアログだけを省略し、走査、警告、取得済み用語の保護、翻訳結果の安全検証は無効にしません。

## 選択外の名称を参照語として保護

選択を外したクエストブック名、チャプターグループ名、チャプター名、クエスト名、タスク名、報酬名、報酬テーブル名、クエストリンク名は、選択中のsubtitle・クエスト詳細・画像hover文から参照される場合に限り、一時的なプロジェクト参照語として原文表記を保護します。

名称自身はOpenAIへ送らず、翻訳先localeにも書きません。subtitleや詳細文そのものは名称候補にしません。raw JSONでは主表示だけを結合し、hover、`with`引数、動的componentを名称に混ぜません。一般語1語や目的文型のtitleは、引用・個別装飾・`chapter` / `quest` 等の参照根拠がない自由文では固定しません。

## 言語keyと公式訳の判定

- item / block / entity / fluid / biome / effect / enchantment / creative tab / dimension / structure等の登録名として扱えるkeyだけを候補にし、quest原文に現れる最長一致を優先します。
- `.desc` / `.description` / tooltip / help / info等の説明keyは候補から外します。親keyがある子keyは、model / blockstateの実在、親表示名を含む短い派生名、または明示的なvariant / type / form / professionを確認できる場合だけ残します。
- 翻訳先値がない、空、または原文と同じ場合は原語を保護します。Mod表示名は同名の言語ファイル訳があっても原文を優先します。
- 公式訳の装飾を除いた可視テキストを使います。表示用printf引数は、残る固定名とlanguage keyのtokenが対応すると確認できる場合だけ除去します。助詞を含む節や状態表示は名前と誤認しません。
- 原文の言語値自体が改行、template、URL等の保護tokenを持つ場合、動的な説明・状態として用語候補から除外します。そのtoken自体は通常の保護処理で保持します。
- 公式訳に原文と一致しない保護tokenがある場合や、制御文字・ゼロ幅文字・空白だけになる場合は原語を保持して警告します。
- `/give` 等のcommand / pathは保護しますが、`/8` のような数値だけのsuffixは他の安全条件を満たす公式訳から除外しません。
- `Iron Ingot` と `Iron Ingots` のような複数語ASCII登録名の安全な規則複数形は同じ用語として照合します。`s`、`y` → `ies`、`ch` / `sh` / `x` / `z` → `es` だけを対象にし、Mod表示名、プロジェクト参照語、競合値、曖昧な1語には適用しません。
- `Good`、`Pressure`、`Storage` のような短い一般語は、自由文や別の長い名前句の一部だけでは固有名詞にしません。用語そのものが開始装飾とresetに囲まれる、引用される、または同じModの強い用語が同時にある場合は保護できます。
- `XP` / `RF` のような2文字のASCII大文字・数字略語も、Mod言語keyに登録されていれば原語のまま保護します。

## resource IDを使う項目内照合

旧版のキー化済みJSONでは、task / reward titleと同じSNBTオブジェクトにある直接のitem / block等のresource IDだけを追加根拠にします。章名、ファイル名、quest icon、近隣taskからは推測しません。

resource IDに対応する正確なMod言語keyがあり、そのtask / reward titleの原文と公式の原文locale値がASCII英数字token単位で完全一致する場合だけ、語順や対応する丸括弧が異なってもその項目内に限って公式訳へ対応付けます。数字はtokenに含め、`16k` と `64k` を同一視しません。同じ翻訳keyが異なるresourceに再利用される場合や、公式候補が競合する場合は関連付けを無効にします。

## 走査上限

設定の「走査上限を有効にする」は既定でONです。OFFにすると4つの数値欄とresetボタンを無効化し、Mod、KubeJS、resource packに表の4上限を適用しません。数値は保持し、ONに戻すと再利用できます。Minecraft本体はこの設定対象ではありません。

| 設定 | 既定値 | 設定可能範囲 |
| --- | ---: | ---: |
| 1資産の項目数上限 | 100,000件 | 1〜1,000,000件 |
| 言語ファイル1件の上限 | 16 MiB | 1〜256 MiB |
| 1資産の言語ファイル合計上限 | 64 MiB | 1〜1,024 MiB |
| 全資産の言語ファイル合計上限 | 512 MiB | 1〜4,096 MiB |

1資産はMod JAR 1件、`kubejs/assets` 全体、またはresource pack 1件です。項目数は、内容を読む言語ファイルだけの件数ではなく、言語・metadataの探索やmodel / blockstate照合のために列挙する項目を数えます。Mod JAR / ZIPはZIP項目一覧の全ファイルと明示的フォルダー、KubeJS / フォルダー型resource packは `assets` 配下の全ファイルと全サブフォルダーが対象です。`mods` と `resourcepacks` 直下の候補探索にも同じ項目数上限を適用します。

言語ファイル1件 ≤ 1資産の言語合計 ≤ 全資産の言語合計の関係を保存時に検証します。合計は原文localeと翻訳先localeの展開後サイズで計算し、ZIPの1ファイル上限は圧縮サイズにも適用します。上限に達した資産の途中結果は採用しません。

## トグルで無効にならない検証

走査上限全体をOFFにしても、次の固定検証は続けます。

- Mod metadata 1ファイル2 MiB
- JSON / `.lang` の言語entry 1ファイル250,000件
- プロジェクト参照名512文字
- Minecraft本体のversion metadata 8 MiB、asset index 32 MiB、client JAR 256 MiB、公式言語JSON 16 MiB、言語entry 250,000件、公式資産path 4,096件
- resource packフォルダーのsymlink / junction、およびresource pack ZIPの構造、central directory宣言entry数と実数、path、重複path、symlink entry
- キャンセルと解析後の入力変更検出

ZIP64 resource packは事前に安全確認できないため走査しません。フォルダー形式ではsymlink / junctionを追跡せず、ZIPでは絶対path、親directory参照、大小文字を無視した重複path、symlink entryを拒否します。

JSONのルート直下でカンマが1個だけ欠け、補完後に全文をstrict JSONとして再解析できる場合に限り、原ファイルを変更せずメモリ上で補完します。それ以外の壊れたJSONや安全に解釈できない言語ファイルは全体を不採用とします。
