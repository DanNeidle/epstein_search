*How to make a private Epstein files search engine - now updated with AI/Claude*
The DOJ search is rubbish. Other public searches don't have all the documents. The solution: spin up your own search engine. Add a bit of AI.

© 2025 Dan Neidle, with Claude/AI elements contributed by SH. 
This work is licensed under the Creative Commons Attribution 4.0 International Licence (CC BY 4.0).
You are free to share and adapt this material for any purpose, including commercially, provided appropriate credit is given.

Requirements:
- a Linux PC or Mac that's reasonably modern, with 32GB+ memory and 200GB+ SSD drive space. 
- Less memory should be okay but you may need to adjust the settings.
- May work on Windows - don't know.

Steps to setup archive and search:

1. go to yung-megafone's Epstein-Files GitHub and use the "magnet" links to download datasets 8 to 12. 
https://github.com/yung-megafone/Epstein-Files

2. If you don't have a torrent downloader you can spin up one - example docker-compose in this repo. 
Surprisingly fast to download if you have a good connection.

3. create a folder: e.g. epstein. Move all the PDFs from all the downloads into a subfolder, epstein/data. Warning: there are 1.3 million PDFs

4. This is not legal advice, but personally I immediately deleted everything left - files that aren't PDFs. 
These include movie files, images etc. I don't want to know what's on them, and I certainly don't want legal liability for having them.

5. Now we need to index the files, make them searchable and create a UI. That's all done with the docker-compose.yml in this repo.

6. Get sist2 to index the files:

docker compose exec -T sist2-admin /root/sist2 scan \
  --output /data/index.sist2 \
  --threads 16 \
  --incremental \
  /documents

This will take around an hour. It will look like it's doing nothing, but it (probably) isn't. 

7. get elasticsearch to digest the index:

docker compose exec -T sist2-admin /root/sist2 index \
  --threads 16 \
  --batch-size 200 \
  --es-url "http://elasticsearch:9200" \
  --incremental-index \
  /data/index.sist2

This will take a few minutes

Then go to localhost:1997 and you have a fantastic UI and search.

*New AI components (for which many thanks to SH*

ep.py wraps the Elasticsearch API into a command line utility. It's primarily designed to be used by AI agents. It has no dependencies, so no need for a venv. Hashes the first 500 chars of content to flag duplicates (many documents appear across multiple Bates ranges). Stops the AI from reporting the same email three times.

With almost no setup, Claude will then undertake research in a remarkably easy and useful way. 

Once sist2/es is working, there are just two more steps to use Claude:

1. Install Claude, using this link [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview)
2. go to your epstein folder and type "claude". Then just ask your question. See claude_example.txt for an example.

Standard AI caveats:
- **OCR quality varies wildly**. Some documents are near-perfect, others are garbled. Always verify findings against the original PDFs via the sist2 links.
- **Document counts are approximate**. A `match` query for "Clinton" will catch incidental mentions, not just substantive connections.
- **The AI can hallucinate connections**. It's good at finding and summarizing what's in the documents, but always check the cited Bates numbers yourself.
- **Duplicate documents** are common across Bates ranges. The near-duplicate detection helps but isn't perfect.

WARNING: if you only search for e.g. Mandleson then you will see nothing (very) disturbing. 
There are, however, very disturbing emails in the files which you may come across by accident. 
There are some images in the PDFs; those I've seen are innocuous (with female faces automatically
redacted) but I can't speak to what else might be there. 

There must be a risk that there are disturbing and potentially illegal images included in the PDFs. 
I've taken advice and am comfortable with my legal position - but this is something you must consider yourself.
Certainly anyone downloading the archive for the *purpose* of finding such material should expect unpleasant legal consequences.
Nothing here constitutes legal advice. 

If you find this useful, please make a donation to TaxAid - the charity that provides free tax advice to people in need. 
https://taxaid.org.uk/support-our-work
