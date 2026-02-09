# Epstein Documents Research Agent

## Data Source
You have access to an Elasticsearch index at `http://localhost:9200` containing 1,046,911 OCR'd PDF documents from the Epstein case files. The index is called `sist2`.

## Document Schema
- `name`: Document filename (e.g. "EFTA02290848") - these are Bates numbers
- `content`: OCR-extracted full text (quality varies, expect typos and garbled text)
- `pages`: Page count
- `size`: File size in bytes
- `extension`: Always "pdf"
- `mtime`: Modified timestamp

## Querying with `ep`

**Always use `./ep.py` for all queries. Do not write raw curl or Python ES queries.**

The `ep` CLI tool handles query construction, output formatting, Bates numbers, clickable sist2 links, highlight snippets, and near-duplicate detection automatically.

### Counting documents
```bash
./ep.py count "Prince Andrew"          # How many docs mention this term?
./ep.py count -c "Andrew" "Epstein"    # Co-occurrence count (both terms must appear)
./ep.py c "Ghislaine"                  # Alias: c = count
```

### Searching with highlights
```bash
./ep.py search "flight log"            # Default 10 results with highlights
./ep.py search -n 20 "Mandelson"       # More results
./ep.py search -f "Ghislaine"          # Fuzzy matching for OCR errors
./ep.py search --min-pages 3 "Musk"    # Only substantive documents
./ep.py s "Prince Andrew"              # Alias: s = search
```

### Co-occurrence search (all terms must appear)
```bash
./ep.py cooccur "Andrew" "Ghislaine" "massage"    # All terms must match
./ep.py co "Fergie" "Epstein" "Buckingham"         # Alias: co = cooccur
```

### Excluding already-seen documents
```bash
./ep.py search -x EFTA02017536 EFTA02028928 "Andrew" "MoS"   # Skip known docs
```

### Reading full documents
```bash
./ep.py read EFTA01777411              # Full content by Bates number
./ep.py read EFTA01777411 --max-chars 5000   # Truncate long docs
./ep.py r EFTA01777411                 # Alias: r = read
```

### Research notes
```bash
./ep.py save "Andrew forwarded Palace PR to Epstein" EFTA02017536 -t andrew -t palace
./ep.py notes                          # View all saved notes
./ep.py notes -t andrew               # Filter by tag
./ep.py tags                           # List all tags
./ep.py n -t fergie                    # Alias: n = notes
```

Notes are persisted to `notes.jsonl` and survive across sessions.

### Typical investigation workflow
1. `./ep.py count "person X"` — gauge the footprint
2. `./ep.py search "person X" "Epstein"` — highlights, 10 results
3. Spot names/events in highlights
4. `./ep.py co "person X" "person Y"` — drill into co-occurrences
5. `./ep.py read EFTA...` — full content of key documents
6. `./ep.py search -x EFTA... "person X" "new keyword"` — follow threads, skip seen docs
7. `./ep.py save "finding" BATES -t tag` — record findings

### Output format
All `ep` output includes: Bates number, page count, clickable sist2 link (`http://localhost:1997/f/{_id}`), highlight snippets, and near-duplicate flags. No JSON parsing needed.

## OCR Quality Notes
- Text is noisy: expect garbled characters, merged words, misrecognised letters
- Names may be misspelled — use `./ep.py search -f` for fuzzy matching and try common OCR errors (l/1, O/0, rn/m)
- Use highlights to verify matches in context before drawing conclusions
- Multi-page documents tend to have richer content; use `--min-pages 3` to filter

## Research Methodology

1. **Start broad**: Search for known key entities (names, companies, locations) to understand what's in the corpus
2. **Follow the threads**: When you find a name or entity, search for it specifically to find all related documents
3. **Cross-reference**: Look for documents where multiple entities of interest appear together
4. **Document types matter**: Look for flight logs, financial records, communications, legal filings — they tell different stories
5. **Build a picture**: Maintain a running summary of key findings, connections, and leads to follow
6. **Verify before concluding**: OCR errors can create false matches — always check highlight context
7. **Note Bates numbers**: Always record the document `name` (Bates number) for any significant finding so it can be verified

## Known Key Entities to Start With
- Jeffrey Epstein, Ghislaine Maxwell, Lesley Groff, Sarah Kellen
- Companies: HBRK Associates, Southern Trust, Financial Trust Company
- Locations: Little St. James, Zorro Ranch, 9 East 71st Street, El Brillo Way
- Aircraft: N908JE (Boeing 727), N212JE (Gulfstream)

## Output Format
When reporting findings:
- Cite documents as: **BATES** ([view](link)) — e.g. **EFTA01777411** ([view](http://localhost:1997/f/a5b85008d965e106267d2348d98b9dc2))
- The `ep` tool outputs Bates numbers and links automatically — copy them into your reports
- Quote relevant text snippets from highlights
- Distinguish between what the document says and your interpretation
- Flag OCR quality issues that might affect reliability
- Use `./ep.py save` to record key findings for later reference
