# Fish Audio Adapter

This adapter renders Alexandria's `speaker` / `text` / `instruct` JSON into one
Fish Audio request and one numbered audio file per entry. It does not modify
Alexandria's local TTS engine.

Install the existing Alexandria Python dependencies if this environment does
not already have them:

```powershell
python -m pip install -r app\requirements.txt
```

Set the API key in the current PowerShell session:

```powershell
$env:FISH_API_KEY = "your-key"
Copy-Item fish_adapter\voices.example.json fish_adapter\voices.json
```

For a persistent local setup, create the ignored `fish_adapter\config.local.json`:

```json
{
  "api": {
    "model": "s2.1-pro-free",
    "api_key": "your-key"
  }
}
```

The environment variable overrides `api.api_key` when both are present. The key
is never written to the manifest or included in log messages.

Fill each `reference_id`, then render the current Alexandria script:

```powershell
python fish_adapter\fish_adapter.py
```

Useful overrides:

```powershell
python fish_adapter\fish_adapter.py --script annotated_script.json --voices fish_adapter\voices.json --output .\books\my-book\audio
python fish_adapter\fish_adapter.py --only 23
```

The default output is `fish_adapter/output`. `manifest.json` is updated after
each segment. Matching fingerprints are skipped on later runs; `--only` always
forces the selected 1-based segment IDs to render again.

## Render a complete EPUB

List the detected content chapters before spending API credits:

```powershell
python tools\render_book.py `
  --epub "D:\code\epub_tts\books\智能简史.epub" `
  --list-chapters
```

Render every chapter and a combined full-book MP3:

```powershell
python tools\render_book.py `
  --epub "D:\code\epub_tts\books\智能简史.epub"
```

Use `--from-chapter` and `--to-chapter` for a range. Re-running the same command
resumes from cached script and audio fingerprints. Use
`--force extract|script|review|audio|merge|all` to rebuild one stage.

Edit `fish_adapter/voice_pool.json` to bind `NARRATOR` and major characters.
Unbound characters rotate through unused pool voices. Assignments are saved in
the book's `voice_assignments.json`, so resumed runs keep the same cast.
