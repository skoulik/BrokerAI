from box import Box
import yaml
from pprint import pprint
from anytree import Node, PreOrderIter, RenderTree
from anytree.exporter import JsonExporter
from langchain_text_splitters import TokenTextSplitter
import chromadb
import chromadb.config
import rag_tools
import asyncio
from transformers import AutoTokenizer

async def main():
    config = Box.from_yaml(
        filename = "config.yaml",
        Loader   = yaml.FullLoader
    )

    spec = Box.from_yaml(
        filename = config.path.pdfs + "spec.yaml",
        Loader   = yaml.FullLoader
    )


    strings_embedder = rag_tools.Embedder(config)
    tokenizer = AutoTokenizer.from_pretrained(config.embeddings.tokenizer_model)

    text = "This is a test!!!" * 80
    text = config.embeddings.document_prefix + text;
    embeddings = await strings_embedder.embed_strings([text])
    print(embeddings[0])
    ntokens = len(tokenizer.encode(text))
    print(f"Text len chars: {len(text)}, tokens: {ntokens}")

    await strings_embedder.close()

asyncio.run(main())
