*How to make a private Epstein files search engine*

The DOJ search is rubbish. Other public searches don't have all the documents.

The solution: spin up your own search engine. 
You'll need a Linux PC or Mac that's reasonably modern, with 32GB+ memory 200GB SSD drive space. 
Less memory should be okay but you may need to adjust the settings.
May work on Windows - don't know.

1. go to yung-megafone's Epstein-Files GitHub and use the "magnet" links to download datasets 8 to 12. 
https://github.com/yung-megafone/Epstein-Files

2. If you don't have a torrent downloader you can spin up one - example docker-compose in this repo. 
Surprisingly fast to download if you have a good connection.

3. create a folder: e.g. epstein. Move all the PDFs from all the downloads into a subfolder, epstein/data. Warning: there are 1.3 million PDFs

4. This is not legal advice, but personally I immediately deleted everything left - files that aren't PDFs. 
These include movie files, images etc. I don't want to know what's on them, and I certainly don't want legal liability for having them.

6. Now we need to index the files, make them searchable and create a UI. That's all done with the docker-compose.yml in this repo.

7. Get sist2 to index the files:

docker compose exec -T sist2-admin /root/sist2 scan \
  --output /data/index.sist2 \
  --threads 16 \
  --incremental \
  /documents

This will take around an hour. It will look like it's doing nothing, but it (probably) isn't. 

5. get elasticsearch to digest the index:

docker compose exec -T sist2-admin /root/sist2 index \
  --threads 16 \
  --batch-size 200 \
  --es-url "http://elasticsearch:9200" \
  --incremental-index \
  /data/index.sist2

This will take a few minutes

Then go to localhost:1997 and you have a fantastic UI and search.

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
