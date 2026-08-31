# Windows単一ファイルのビルドとRelease

`.github/workflows/package-release.yml` は、Windows x64向けGUIをPyInstallerで単一の実行ファイルにします。

```text
MinecraftQuestLocalizer-v<version>-windows-x64.exe
```

実行ファイルにはPythonインタープリター、標準ライブラリ、Tk / Tcl、アプリケーションコードを含みます。現状は追加data file、icon、hidden importを必要としません。

## 起動条件

workflowは次の場合に起動します。

- `master`へのpushに `src/**`、`launch.pyw`、`pyproject.toml` のいずれかが含まれる
- package / Release用workflowまたは判定scriptが変更される
- GitHub Actions画面から手動実行する

README、`docs/**`、通常の`tests/**`、LICENSEだけの変更では起動しません。workflowや判定scriptだけの変更で起動した場合も、前回のVersion付きRelease以降に実アプリの差分がなければbuild jobを実行しません。

実アプリの差分として扱う範囲は `src/**`、`launch.pyw`、`pyproject.toml` です。将来icon、PyInstaller spec、実行時assetを追加した場合は、workflowの起動pathとrelease plannerの実アプリpathへ追加する必要があります。

## バージョン

リリース版の基準は `pyproject.toml` の `[project].version` です。同じ値を `src/mq_localizer/__init__.py` の `__version__` にも記載します。

```toml
[project]
version = "1.0.0"
```

```python
__version__ = "1.0.0"
```

両方は完全に一致し、有効かつ正規表記のPEP 440バージョンでなければなりません。通常テストとrelease plannerの両方が不一致を拒否します。バージョン更新時は必ず2ファイルを同時に変更してください。

## build判定

公開済み・非draftで、tagが `v<PEP 440 version>` または `<PEP 440 version>` のReleaseを全ページ取得します。その中の最大バージョンを「前回のVersion付きRelease」とし、対応tagのcommitから現在のcommitまでを比較します。

- 前回Release以降に実アプリ差分がある場合だけ、全テストと単一EXE buildを実行します。
- Version付きReleaseがまだない場合は初回buildとして扱います。
- `nightly` 等のVersionとして解釈できないRelease tagは比較対象外です。
- 同じ最大バージョンを表す公開Releaseが複数ある場合や、Release tagとそのcommit内の `pyproject.toml` が一致しない場合は、安全に判定できないため停止します。

buildしたEXEは14日間のActions artifactとして、ZIPへ包まず1ファイルのまま保存します。

## Release判定

build成功後、次の条件をすべて満たす場合だけGitHub Releaseを作成します。

1. 実アプリに変更がある
2. `pyproject.toml` と `__version__` が一致する
3. 現在のバージョンが前回Releaseから更新されている
4. 現在のバージョンが公開済みの最大バージョンより新しい
5. 同名のdraft Releaseがない
6. `v<version>` tagが未作成、または現在のcommitを指している
7. `master`上で全テストと単一EXE buildが成功している

Release作成直前にRelease一覧と条件を再確認します。対象tagは現在のcommitを指すことを確認して非forceで先にpushし、リモート上のtagが一致する場合だけReleaseを作成します。別commitを指す同名tagは移動せず、同じバージョンを表すdraftも変更・公開しません。pre-release / development versionはGitHub側でもpre-releaseとして作成します。

## 権限

plan jobとbuild jobはrepository contentの読み取り権限だけを使います。GitHub Releaseとtagを作る `contents: write` はrelease jobだけに付与し、repository内で自動生成される `GITHUB_TOKEN` を使用します。利用する公式Actionは検証済みcommit SHAへ固定しています。

公開後を含めてVersion tagを固定するには、GitHubのrepository rulesetで `v*` tagの更新・削除を禁止する運用を推奨します。workflow自身はforce pushを行わず、競合を検出した場合は停止します。

## ローカルで同じbuildを行う

Python 3.11 x64とPyInstaller 6.22.2を使用します。

```powershell
py -3.11 -m pip install "pyinstaller==6.22.2"
py -3.11 -m PyInstaller --noconfirm --clean --onefile --windowed --name MinecraftQuestLocalizer --paths src launch.pyw
```

出力は `dist/MinecraftQuestLocalizer.exe` です。

参考資料:

- [PyInstaller: Bundling to One File](https://pyinstaller.org/en/stable/operating-mode.html#bundling-to-one-file)
- [GitHub Actions workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
- [GitHub CLI: `gh release create`](https://cli.github.com/manual/gh_release_create)
