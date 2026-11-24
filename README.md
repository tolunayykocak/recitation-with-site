# recitation-with-site

Public site of the Recitation With app, served by GitHub Pages:
`https://tolunayykocak.github.io/recitation-with-site/`

| Path | What |
|---|---|
| `catalog/reciters.json` | Reciter catalog: `schemaVersion`, `revision`, reciters |
| `catalog/manifests/<id>.json` | Audio manifests (ayah durations, surah-file timestamps) |
| `catalog/avatars/<id>.webp` | Optional reciter photos, 512×512 WebP |
| `catalog/translations/translations.json` | Quran translations (meal) index: id, language, translator, QuranEnc version, file + sha256 |
| `catalog/translations/<id>.json` | One translation from QuranEnc.com, unchanged: 114 surahs, 6236 ayahs, footnotes |
| `tools/catalog.py` | Validator (`check`); copied from the app repo |
| `tools/translations.py` | Translations validator (`check`); copied from the app repo |

The app checks `catalog/` when it opens, at most once a day, and keeps an embedded copy.
**Do not edit `catalog/` here.** The master copy lives in the app repo
(`app/src/main/assets/catalog/`); its *Audio manifest* workflow opens pull requests here.
Merging one publishes it (`.github/workflows/pages.yml` validates, then deploys).
`catalog/translations/` comes the same way from the app repo's *Translations* workflow
(QuranEnc.com texts, republished without change, source named, current version followed).

How to add a reciter: `docs/okuyucu-ekleme.md` in the app repo.
