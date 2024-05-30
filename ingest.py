import os
import glob
from box import Box
import yaml
from pprint import pprint
from anytree import PreOrderIter, RenderTree
from anytree.exporter import JsonExporter
from anytree.importer import JsonImporter
from langchain_text_splitters import RecursiveCharacterTextSplitter
import httpx
import chromadb
from chromadb.config import Settings as ChromaDB_Settings
import pdf_tools


config = Box.from_yaml(
    filename = "config.yaml",
    Loader   = yaml.FullLoader
)


##
# Convert PDFs to trees
##

pdfs_fnames = [os.path.basename(f) for f in glob.glob(config.path.pdfs + "*.pdf")]

tree_importer = JsonImporter()
tree_exporter = JsonExporter(
    indent         = None,
    sort_keys      = False,
    ensure_ascii   = False,
    check_circular = False
)
trees = {}

for pdf_fname in pdfs_fnames:
    pdf_name  = config.path.pdfs  + pdf_fname
    tree_name = config.path.trees + pdf_fname + ".json"
    if os.path.isfile(tree_name):
        fh = open(tree_name, "r")
        trees[pdf_fname] = tree_importer.read(fh)
        fh.close()
    else:
        trees[pdf_fname] = pdf_tools.pdf_to_tree(
            file_name      = pdf_name,
            page_numbers   = None,
            detect_columns = True
        )
        fh = open(tree_name, "w")
        tree_exporter.write(trees[pdf_fname], fh)
        fh.close()


##
# Chunk and embed
##

text_splitter = RecursiveCharacterTextSplitter(
    chunk_size    = config.chunker.size,
    chunk_overlap = config.chunker.overlap,
    separators    = config.chunker.separators
)

embeddings_http_client = httpx.Client(
    base_url = config.embeddings.base_url,
    timeout  = config.embeddings.timeout,
    headers={'Accept': "application/json"}
)

chromadb_client = chromadb.PersistentClient(
    path     = config.path.embeddings,
    settings = ChromaDB_Settings(anonymized_telemetry = False)
)

collections = {}

for pdf_fname in pdfs_fnames:

    if pdf_fname in [c.name for c in chromadb_client.list_collections()]:
        collections[pdf_fname] = chromadb_client.get_collection(name=pdf_fname)
        continue

    collections[pdf_fname] = chromadb_client.create_collection(
        name     = pdf_fname,
        metadata = {"hnsw:space": "cosine"}
    )

    for node in PreOrderIter(trees[pdf_fname]):
        text = node.header + "\n" + node.text
        if text == "" or text.isspace(): continue
        path = '/'.join([n.name for n in node.path])
        splits = text_splitter.split_text(text)
        print(f"Embedding: {path}...\n")
        request_json = config.embeddings.template
        request_json['input'] = splits
        response = embeddings_http_client.post(
            url  = config.embeddings.endpoint,
            json = request_json
        )
        collections[pdf_fname].add(
            embeddings = [r['embedding'] for r in response.json()['data']],
            #metadatas  = [{...}],
            ids        = [path + "#" + str(i) for i in range(len(splits))]
        )

embeddings_http_client.close()