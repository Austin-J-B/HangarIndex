# HangarIndex

HangarIndex is a local-first proof of concept for searching aerospace maintenance documents and asking a language model questions grounded in matching passages. The public corpus includes the original FAA starter folder plus regulatory, repair-practice, science, parts, supplier, and management references. Source registers record provenance and redistribution status; they are not indexed. HangarIndex helps people locate source documents and cross-references in a company-public library, not approve repairs or replace controlled technical data.

## Start on Windows

1. Install [Python 3.11 or newer](https://www.python.org/downloads/).
2. Open PowerShell in this folder and run:

   ```powershell
   .\run.ps1
   ```

3. Open <http://127.0.0.1:8000> in your browser.
4. The source folder defaults to `corpus\FAA-Public`. Choose **Index this folder** to build its local index.
5. Later, replace the folder path with a mounted company-public folder, for example `Z:\Public\Maintenance`, and index that folder.

If PowerShell blocks the script, run `powershell -ExecutionPolicy Bypass -File .\run.ps1` from this folder.

## Optional phone access on Tailscale

The server stays on localhost unless configured. To use it from a phone on your private Tailscale network, add these settings to `.env`, replacing the example address and hostname with this computer's assigned Tailscale IPv4 and MagicDNS name:

```dotenv
HANGAR_BIND=100.x.y.z
HANGAR_ACCESS_TOKEN=choose-a-long-random-passphrase
HANGAR_ALLOWED_HOSTS=100.x.y.z,hangar-pc.your-tailnet.ts.net
```

`HANGAR_BIND` accepts localhost or an assigned IPv4 in Tailscale's `100.64.0.0/10` range; other network binds are rejected. Tailnet mode requires a passphrase of at least 16 characters and checks the exact Host name or IP on every request. Failed logins use an increasing per-device delay. Open `http://hangar-pc.your-tailnet.ts.net:8000` on a device in that tailnet and enter the passphrase; the browser receives an HttpOnly session cookie. Keep access within a trusted tailnet. PDFs open in the browser; other source files can only be opened by the server computer's local applications.

On Linux or macOS, run `./run.sh`. The app binds to `127.0.0.1` by default so the development UI is not exposed to the network.

## Local language model

Search works without a language model. For cited generated answers, install [Ollama for Windows](https://ollama.com/download/windows) on the same computer, open PowerShell, and download the default Thorough model:

```powershell
ollama pull qwen3.5:35b-a3b
```

Ollama normally runs in the background after installation. Copy `.env.example` to `.env` if you want to change settings, then start HangarIndex with `.\run.ps1` and open <http://127.0.0.1:8000>. Thorough (`qwen3.5:35b-a3b`) is the default, with thinking disabled. Quick (`qwen3.5:4b`) is an opt-in faster but less reliable mode; pull it before selecting Quick:

```powershell
ollama pull qwen3.5:4b
```

Both modes use thinking disabled and a 30-minute keep-alive. Configure the models with `OLLAMA_QUICK_MODEL` and `OLLAMA_THOROUGH_MODEL` in `.env`. Claude's blind caution review found roughly three times as many errors from Quick and missed all three reviewed caution cases; the UI labels Quick as less reliable. The optional smallest mode is `llama3.2:3b`. The app sends Ollama only retrieved excerpts and source names over localhost; document search itself is handled by the local index.

## Hybrid retrieval

Search uses SQLite FTS5 and local semantic ranking by default. The vector rank receives twice the lexical weight in reciprocal-rank fusion. Install the embedding model with:

```powershell
ollama pull nomic-embed-text
```

At server startup, the app builds a private NumPy cosine cache in the selected local data directory in the background. Searches continue with FTS5 while that cache warms; a failed refresh retries with capped backoff while FTS5 remains available. Once ready, the app fuses semantic and lexical ranks. Set `HANGAR_EMBEDDINGS=0` in `.env` to use FTS5 only. The cache is refreshed after reindexing and rebuilt for a new index generation or embedding model.

If Ollama is not installed or running, file search still works. The Ask page will show retrieved source excerpts and explain that model-generated answers are unavailable.

An OpenAI-compatible endpoint can be configured by setting `LLM_PROVIDER=openai`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL` in `.env`. Retrieved document excerpts are sent to that endpoint for answer generation. Use this option only if company policy permits it.

## What it indexes

- PDF, DOCX, PPTX, XLSX, TXT, Markdown, CSV, HTML, XML, and LOG files.
- The starter set demonstrates repair-station rules and manuals, part eligibility and records, aluminum conversion coatings, titanium alloy properties, aerospace corrosion research, coating qualification/product references, and public supplier context. Source dates, revision cues, and authority links are in [the source register](corpus/FAA-Public/SOURCE-REGISTER.md).
- PDF pages are kept as page citations; PowerPoint slide and spreadsheet sheet numbers are retained as a location hint.
- `SOURCE-REGISTER.md` files are kept as provenance metadata and skipped by indexing.
- The index uses SQLite FTS5 ranking and filename/folder/type weighting, fused with a local NumPy vector ranker by default. Set `HANGAR_EMBEDDINGS=0` for FTS5 only. OCR and scanned-PDF processing are not included.
- Files are read from their source folder and chunk text is stored in `.data\index.sqlite3`. Original source files are not copied into the index. Incremental runs skip unchanged files.
- The first run over a large share may take a while. Point the app at a narrow set of folders first. Avoid indexing the whole terabyte drive until you have set a manageable scope and reviewed local storage and indexing time.
- The FAA general maintenance handbook is about 92 MB, so the first index of the starter folder can take a few minutes depending on the computer.
- Legacy `.doc`, `.xls`, and `.ppt`, image-only PDFs, CAD files, and audio/video are not supported. PDFs larger than 120 MiB are skipped by default. The limit can be changed with `MAX_FILE_BYTES`.

## What it does not do

- It does not access a Windows share unless the app is running on a machine where that path is mounted and the running user has permission.
- It does not determine effectivity, supersedure, or whether an instruction is approved for a specific article. Company source libraries can contain obsolete and non-applicable files; check revision, serial/part applicability, authority, and current controlled copies.
- It does not guarantee that an external model will follow the prompt or cite perfectly. The assistant sends only retrieved excerpts and asks the model to cite each answer. Always open and verify the cited source.
- In default localhost mode it has no login. Optional Tailscale access uses one shared passphrase, not per-user access control; do not expose the development server to the public internet.

## Layout

```text
HangarIndex/
  app.py                   FastAPI service, parsing, FTS retrieval, model adapter
  web/                     Local search and assistant UI
  corpus/FAA-Public/       Public FAA, EASA, AESA, CAAC, TCCA, materials, and supplier references
  .data/                   Local SQLite index and folder setting, created on first run
```

Starter-source details and official links are in [corpus/README.md](corpus/README.md) and [corpus/FAA-Public/SOURCE-REGISTER.md](corpus/FAA-Public/SOURCE-REGISTER.md).
