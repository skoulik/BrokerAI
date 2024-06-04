from box import Box
import yaml
from pprint import pprint
from anytree import Node, PreOrderIter, RenderTree
from anytree.exporter import JsonExporter
from langchain_text_splitters import RecursiveCharacterTextSplitter
import chromadb
from chromadb.config import Settings as ChromaDB_Settings
import rag_tools
import asyncio

async def main():
    config = Box.from_yaml(
        filename = "config.yaml",
        Loader   = yaml.FullLoader
    )

    ##
    # Convert PDFs to trees
    ##

    (pdfs, trees) = await asyncio.gather(rag_tools.get_documents(config), rag_tools.get_trees(config))

    tree_exporter = JsonExporter(
        indent         = None,
        sort_keys      = False,
        ensure_ascii   = False,
        check_circular = False
    )

    for pdf in pdfs:
        if pdf['id'] in trees: continue
    
        pdf_name  = config.path.pdfs  + pdf['file_name']
        tree_name = config.path.trees + pdf['file_name'] + ".json"
        trees[pdf['id']] = rag_tools.pdf_to_tree(
            file_name      = pdf_name,
            page_numbers   = None, #TODO
            detect_columns = True
        )
        fh = open(tree_name, "w")
        tree_exporter.write(trees[pdf['id']], fh)
        fh.close()

    ##
    # Chunk and embed
    ##

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size    = config.chunker.size,
        chunk_overlap = config.chunker.overlap,
        separators    = config.chunker.separators
    )

    strings_embedder = rag_tools.Embedder(config)

    chromadb_client = chromadb.PersistentClient(
        path     = config.path.embeddings,
        settings = ChromaDB_Settings(anonymized_telemetry = False)
    )

    collections = {}

    for pdf in pdfs:  

        if pdf['id'] in [c.name for c in chromadb_client.list_collections()]:
            collections[pdf['id']] = chromadb_client.get_collection(name=pdf['id'])
            continue

        collections[pdf['id']] = chromadb_client.create_collection(
            name     = pdf['id'],
            metadata = {"hnsw:space": "cosine"}
        )

        async def embed_node(node: Node):
            text = node.header + "\n" + node.text
            if text == "" or text.isspace(): return
            path = '/'.join([n.name for n in node.path])
            splits = text_splitter.split_text(text)
            print(f"Embedding: {path}...")
            embeddings = await strings_embedder.embed_strings(splits)
            collections[pdf['id']].add(
                embeddings = embeddings,
                #metadatas  = [{...}],
                ids        = [path + "#" + str(i) for i in range(len(splits))]
            )
        for embed in asyncio.as_completed([embed_node(node) for node in PreOrderIter(trees[pdf['id']])]):
            await embed

    await strings_embedder.close()

asyncio.run(main())
